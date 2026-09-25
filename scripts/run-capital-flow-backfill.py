from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from astock_backtester.data.cache import LocalCache
from astock_backtester.data.capital_flow_crawler import CapitalFlowCrawler
from astock_backtester.data.operations import fetch_capital_flow_into_cache
from astock_backtester.data.providers import (
    ADataProvider,
    AkshareProvider,
    CompositeProvider,
    HttpAStockProvider,
)
from astock_backtester.data.symbols import normalize_symbol
from astock_backtester.data.warehouse import KNOWN_CAPITAL_FLOW_SOURCE_GAP_DATES, Warehouse

DEFAULT_BATCH_SIZE = 20
DEFAULT_MAX_WORKERS = 4
DEFAULT_TIMEOUT = 15


def default_cache_dir() -> Path:
    return PROJECT_ROOT / "\u8fd0\u884c\u4ea7\u7269" / "\u672c\u5730\u6570\u636e\u4ed3"


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def unique_symbols(symbols: Sequence[object]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for symbol in symbols:
        code = normalize_symbol(symbol)
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def load_completed_symbols(progress_path: Path) -> set[str]:
    completed: set[str] = set()
    if not progress_path.exists():
        return completed
    for line in progress_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        completion_kind = str(event.get("completion_kind") or "")
        if event.get("status") == "completed" and (
            int(event.get("imported_rows") or 0) > 0
            or completion_kind in {"already_complete", "known_source_gap"}
        ):
            symbol = event.get("symbol")
            if symbol:
                completed.add(str(symbol))
    return completed


def select_backfill_symbols(
    frame: pd.DataFrame,
    explicit_symbols: Sequence[object],
    completed_symbols: set[str],
    limit: int,
    provider_symbols: Sequence[object] | None = None,
) -> list[str]:
    if explicit_symbols:
        symbols = unique_symbols(explicit_symbols)
    elif not frame.empty and {"symbol", "main_net_inflow"}.issubset(frame.columns):
        missing = frame.loc[frame["main_net_inflow"].isna(), "symbol"]
        symbols = sorted(unique_symbols(missing.dropna().tolist()))
    elif not frame.empty and "symbol" in frame.columns:
        symbols = sorted(unique_symbols(frame["symbol"].dropna().tolist()))
    else:
        symbols = []

    if not explicit_symbols and provider_symbols:
        seen = set(symbols)
        for symbol in sorted(unique_symbols(provider_symbols)):
            if symbol not in seen:
                symbols.append(symbol)
                seen.add(symbol)

    symbols = [symbol for symbol in symbols if symbol not in completed_symbols]
    if limit > 0:
        return symbols[:limit]
    return symbols


def select_missing_symbols_from_warehouse(
    warehouse: Warehouse,
    completed_symbols: set[str],
    start_date: str,
    end_date: str,
    limit: int,
) -> list[str]:
    paths = sorted(
        warehouse.daily_bars_root.glob("year=*/daily_bars.parquet"),
        reverse=True,
    )
    selected: list[str] = []
    seen: set[str] = set(completed_symbols)
    wanted = ["symbol", "trade_date", "open", "high", "low", "close", "main_net_inflow"]
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    for path in paths:
        try:
            available = set(pq.ParquetFile(path).schema_arrow.names)
        except Exception:
            continue
        columns = [column for column in wanted if column in available]
        if "symbol" not in columns or "trade_date" not in columns:
            continue
        try:
            frame = pd.read_parquet(path, columns=columns)
        except Exception:
            continue
        if frame.empty:
            continue
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        frame = frame.loc[(frame["trade_date"] >= start) & (frame["trade_date"] <= end)]
        ohlc_columns = [column for column in ["open", "high", "low", "close"] if column in frame]
        if len(ohlc_columns) == 4:
            frame = frame.loc[frame[ohlc_columns].notna().all(axis=1)]
        elif ohlc_columns:
            frame = frame.iloc[0:0]
        if frame.empty:
            continue
        if "main_net_inflow" not in frame:
            missing_symbols = unique_symbols(frame["symbol"].dropna().tolist())
        else:
            missing = frame.loc[
                frame["main_net_inflow"].isna()
                & ~frame["trade_date"].isin(KNOWN_CAPITAL_FLOW_SOURCE_GAP_DATES),
                "symbol",
            ]
            missing_symbols = unique_symbols(missing.dropna().tolist())
        for symbol in sorted(missing_symbols):
            if symbol in seen:
                continue
            seen.add(symbol)
            selected.append(symbol)
            if limit > 0 and len(selected) >= limit:
                return selected
    return selected


def load_provider_symbols() -> list[str]:
    try:
        provider = CompositeProvider([ADataProvider(), AkshareProvider(), HttpAStockProvider()])
        return sorted(unique_symbols(provider.list_symbols()))
    except Exception:
        return []


def operation_to_dict(result: object) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    if isinstance(result, dict):
        return result
    return {
        "status": "unknown",
        "imported_rows": 0,
        "failures": [{"code": "unexpected_result", "message": repr(result)}],
    }


def diagnostics_should_skip_eastmoney(diagnostics: Sequence[object]) -> bool:
    return any(
        isinstance(item, dict)
        and item.get("code") == "provider_attempt_failed"
        and item.get("provider") == "eastmoney"
        and item.get("error_code") == "network_error"
        for item in diagnostics
    )


def chunks(items: Sequence[str], size: int) -> list[list[str]]:
    chunk_size = max(1, size)
    return [list(items[index : index + chunk_size]) for index in range(0, len(items), chunk_size)]


def operation_completion(result: dict[str, Any], symbol: str) -> tuple[bool, str, list[dict[str, Any]], list[dict[str, Any]]]:
    rows = int(result.get("imported_rows") or 0)
    failures = result.get("failures") if isinstance(result.get("failures"), list) else []
    diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), list) else []
    missing_symbols = result.get("missing_symbols") if isinstance(result.get("missing_symbols"), list) else []
    normalized_failures = [item for item in failures if isinstance(item, dict)]
    normalized_diagnostics = [item for item in diagnostics if isinstance(item, dict)]
    incomplete = [
        item
        for item in normalized_diagnostics
        if item.get("code") == "date_coverage_shortfall"
        and item.get("provider") != "sina"
        and str(item.get("symbol") or symbol) == symbol
    ]
    if normalized_failures or incomplete or symbol in {str(item) for item in missing_symbols}:
        return False, "incomplete", normalized_failures, normalized_diagnostics
    if any(
        item.get("code") == "capital_flow_known_source_gap_remaining"
        for item in normalized_diagnostics
    ):
        return True, "known_source_gap", normalized_failures, normalized_diagnostics
    if any(item.get("code") == "capital_flow_backfill_not_needed" for item in normalized_diagnostics):
        return True, "already_complete", normalized_failures, normalized_diagnostics
    if str(result.get("status") or "") == "ok" or rows > 0:
        return True, "imported", normalized_failures, normalized_diagnostics
    return False, "incomplete", normalized_failures, normalized_diagnostics


def symbol_row_summary(batch_result: dict[str, Any], symbol: str) -> tuple[int, int]:
    imported_rows: int | None = None
    returned_rows: int | None = None
    diagnostics = batch_result.get("diagnostics") if isinstance(batch_result.get("diagnostics"), list) else []
    for item in diagnostics:
        if not isinstance(item, dict):
            continue
        if item.get("code") != "capital_flow_symbol_summary":
            continue
        if str(item.get("symbol") or "") != symbol:
            continue
        imported_rows = int(item.get("imported_rows") or 0)
        returned_rows = int(item.get("returned_rows") or 0)
        break

    rows = batch_result.get("rows") if isinstance(batch_result.get("rows"), list) else []
    if returned_rows is None and rows:
        seen_dates: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or str(row.get("symbol") or "") != symbol:
                continue
            trade_date = str(row.get("trade_date") or "")
            if not trade_date or trade_date in seen_dates:
                continue
            seen_dates.add(trade_date)
        returned_rows = len(seen_dates)

    fetched_symbols = {str(item) for item in batch_result.get("fetched_symbols", []) if item is not None}
    if imported_rows is None:
        imported_rows = returned_rows if returned_rows is not None else (1 if symbol in fetched_symbols else 0)
    if returned_rows is None:
        returned_rows = 1 if symbol in fetched_symbols else 0
    return imported_rows, returned_rows


def symbol_operation_result(batch_result: dict[str, Any], symbol: str) -> dict[str, Any]:
    fetched_symbols = {str(item) for item in batch_result.get("fetched_symbols", []) if item is not None}
    skipped_symbols = {str(item) for item in batch_result.get("skipped_symbols", []) if item is not None}
    missing_symbols = {str(item) for item in batch_result.get("missing_symbols", []) if item is not None}
    failures = [
        item
        for item in batch_result.get("failures", [])
        if isinstance(item, dict) and str(item.get("symbol") or symbol) == symbol
    ]
    diagnostics = [
        item
        for item in batch_result.get("diagnostics", [])
        if isinstance(item, dict) and (not item.get("symbol") or str(item.get("symbol")) == symbol)
    ]
    imported_rows, returned_rows = symbol_row_summary(batch_result, symbol)
    is_failed = symbol in missing_symbols or bool(failures) or any(
        item.get("code") == "date_coverage_shortfall"
        and item.get("provider") != "sina"
        and str(item.get("symbol") or symbol) == symbol
        for item in diagnostics
    )
    if symbol in skipped_symbols and not is_failed:
        diagnostics = [
            *diagnostics,
            {
                "code": "capital_flow_backfill_not_needed",
                "symbol": symbol,
            },
        ]
    if symbol not in fetched_symbols and symbol not in skipped_symbols and not is_failed:
        missing_symbols.add(symbol)
        diagnostics = [
            *diagnostics,
            {
                "code": "capital_flow_symbol_missing_from_batch_result",
                "symbol": symbol,
                "message": "Batch result did not report this symbol as fetched, skipped, or failed.",
            },
        ]
        is_failed = True
    return {
        "status": "partial" if is_failed else "ok",
        "imported_rows": imported_rows,
        "returned_rows": returned_rows,
        "fetched_symbols": [symbol] if symbol in fetched_symbols else [],
        "missing_symbols": [symbol] if symbol in missing_symbols else [],
        "skipped_symbols": [symbol] if symbol in skipped_symbols else [],
        "failures": failures,
        "diagnostics": diagnostics,
    }


def run_supervised_backfill(
    symbols: Sequence[str],
    run_symbol: Callable[[str], object],
    progress_path: Path,
    max_consecutive_failures: int,
    sleep_seconds: float,
    run_batch: Callable[[list[str]], object] | None = None,
    batch_size: int = 1,
) -> dict[str, Any]:
    started_at = datetime.now().isoformat(timespec="seconds")
    append_jsonl(
        progress_path,
        {
            "event": "start",
            "time": started_at,
            "total_symbols": len(symbols),
            "max_consecutive_failures": max_consecutive_failures,
        },
    )
    processed = 0
    completed = 0
    failed = 0
    imported_rows = 0
    returned_rows = 0
    consecutive_failures = 0
    stopped_reason: str | None = None

    symbol_batches = chunks(list(symbols), batch_size)
    for batch_start, batch in enumerate(symbol_batches, start=0):
        started = time.time()
        batch_imported_rows = 0
        batch_returned_rows = 0
        batch_status_code: object = None
        if run_batch is not None:
            try:
                batch_result = operation_to_dict(run_batch(batch))
                batch_imported_rows = int(batch_result.get("imported_rows") or 0)
                batch_returned_rows = int(batch_result.get("returned_rows") or 0)
                batch_status_code = batch_result.get("status")
                batch_results = {
                    symbol: symbol_operation_result(batch_result, symbol)
                    for symbol in batch
                }
            except Exception as exc:
                batch_results = {
                    symbol: {
                        "status": "partial",
                        "imported_rows": 0,
                        "failures": [{"symbol": symbol, "code": "batch_error", "message": str(exc)}],
                        "diagnostics": [],
                    }
                    for symbol in batch
                }
        else:
            batch_results = {}
            for symbol in batch:
                try:
                    batch_results[symbol] = operation_to_dict(run_symbol(symbol))
                except Exception as exc:
                    batch_results[symbol] = {
                        "status": "partial",
                        "imported_rows": 0,
                        "failures": [{"symbol": symbol, "code": "symbol_error", "message": str(exc)}],
                        "diagnostics": [],
                    }

        if run_batch is not None:
            imported_rows += batch_imported_rows
            returned_rows += batch_returned_rows
            append_jsonl(
                progress_path,
                {
                    "event": "batch",
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "index": batch_start + 1,
                    "total_batches": len(symbol_batches),
                    "symbols": batch,
                    "status_code": batch_status_code,
                    "imported_rows": batch_imported_rows,
                    "returned_rows": batch_returned_rows,
                    "seconds": round(time.time() - started, 3),
                },
            )

        for offset, symbol in enumerate(batch, start=1):
            index = batch_start * max(1, batch_size) + offset
            result = batch_results[symbol]
            rows = int(result.get("imported_rows") or 0)
            is_complete, completion_kind, failures, diagnostics = operation_completion(result, symbol)
            processed += 1
            if run_batch is None:
                imported_rows += rows
                returned_rows += int(result.get("returned_rows") or 0)
            if is_complete:
                completed += 1
                consecutive_failures = 0
                append_jsonl(
                    progress_path,
                    {
                        "status": "completed",
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "symbol": symbol,
                        "index": index,
                        "total_symbols": len(symbols),
                        "imported_rows": rows,
                        "returned_rows": int(result.get("returned_rows") or 0),
                        "completion_kind": completion_kind,
                        "status_code": result.get("status"),
                        "failure_count": len(failures),
                        "seconds": round(time.time() - started, 3),
                    },
                )
            else:
                failed += 1
                consecutive_failures += 1
                append_jsonl(
                    progress_path,
                    {
                        "status": "failed",
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "symbol": symbol,
                        "index": index,
                        "total_symbols": len(symbols),
                        "status_code": result.get("status"),
                        "failures": failures[:3],
                        "diagnostics": diagnostics[:3],
                        "consecutive_failures": consecutive_failures,
                        "seconds": round(time.time() - started, 3),
                    },
                )

            if max_consecutive_failures > 0 and consecutive_failures >= max_consecutive_failures:
                stopped_reason = "max_consecutive_failures"
                append_jsonl(
                    progress_path,
                    {
                        "event": "stopped",
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "reason": stopped_reason,
                        "consecutive_failures": consecutive_failures,
                        "processed": processed,
                        "total_symbols": len(symbols),
                    },
                )
                break
        if stopped_reason is not None:
            break
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    if stopped_reason is None:
        append_jsonl(
            progress_path,
            {
                "event": "finish",
                "time": datetime.now().isoformat(timespec="seconds"),
                "processed": processed,
                "total_symbols": len(symbols),
                "completed": completed,
                "failed": failed,
                "imported_rows": imported_rows,
                "returned_rows": returned_rows,
            },
        )

    return {
        "processed": processed,
        "total_symbols": len(symbols),
        "completed": completed,
        "failed": failed,
        "imported_rows": imported_rows,
        "returned_rows": returned_rows,
        "stopped_reason": stopped_reason,
        "progress_path": str(progress_path),
    }


def read_existing_daily_bars(cache: LocalCache, warehouse: Warehouse, start_date: str, end_date: str) -> pd.DataFrame:
    frame = warehouse.read_daily_bars(start_date=start_date, end_date=end_date)
    if not frame.empty:
        return frame
    frame = cache.read_daily_bars()
    if frame.empty:
        return frame
    frame = frame.loc[
        (frame["trade_date"] >= pd.Timestamp(start_date))
        & (frame["trade_date"] <= pd.Timestamp(end_date))
    ]
    return frame.reset_index(drop=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", default=str(default_cache_dir()))
    parser.add_argument("--start-date", default="2015-01-01")
    parser.add_argument("--end-date", default=pd.Timestamp.today().date().isoformat())
    parser.add_argument("--symbols", nargs="*", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--skip-eastmoney", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--sleep-seconds", type=float, default=0.5)
    parser.add_argument("--max-consecutive-failures", type=int, default=5)
    parser.add_argument("--progress-path", default="")
    parser.add_argument("--no-provider-symbols", action="store_true")
    return parser


def run_backfill_from_args(argv: Sequence[str] | None = None) -> dict[str, Any]:
    parser = build_parser()
    args = parser.parse_args(argv)

    cache_dir = Path(args.cache_dir)
    progress_path = Path(args.progress_path) if args.progress_path else cache_dir / "capital-flow-progress.jsonl"
    cache = LocalCache(cache_dir)
    warehouse = Warehouse(cache_dir)
    completed = load_completed_symbols(progress_path) if args.resume else set()
    if args.symbols:
        symbols = select_backfill_symbols(
            pd.DataFrame(),
            args.symbols,
            completed,
            args.limit,
        )
    else:
        symbols = select_missing_symbols_from_warehouse(
            warehouse,
            completed,
            args.start_date,
            args.end_date,
            args.limit,
        )
        provider_symbols = [] if args.no_provider_symbols else load_provider_symbols()
        if not symbols and provider_symbols:
            seen = set(symbols)
            for symbol in sorted(unique_symbols(provider_symbols)):
                if symbol in seen or symbol in completed:
                    continue
                symbols.append(symbol)
                seen.add(symbol)
                if args.limit > 0 and len(symbols) >= args.limit:
                    break
        if not symbols:
            existing = read_existing_daily_bars(cache, warehouse, args.start_date, args.end_date)
            symbols = select_backfill_symbols(
                existing,
                [],
                completed,
                args.limit,
                provider_symbols=provider_symbols,
            )
    crawler = CapitalFlowCrawler()
    skip_eastmoney = bool(args.skip_eastmoney)

    def run_batch(symbol_batch: list[str]) -> object:
        nonlocal skip_eastmoney
        result = fetch_capital_flow_into_cache(
            cache=cache,
            warehouse=warehouse,
            capital_flow_fetcher=lambda symbols, start_date, end_date: crawler.fetch_many_fund_flows(
                list(symbols),
                start_date,
                end_date,
                skip_eastmoney=skip_eastmoney,
                timeout=max(1, int(args.timeout)),
                max_workers=max(1, int(args.max_workers)),
            ),
            symbols=symbol_batch,
            start_date=args.start_date,
            end_date=args.end_date,
            refresh_coverage=False,
        )
        diagnostics = getattr(result, "diagnostics", [])
        if diagnostics_should_skip_eastmoney(diagnostics):
            skip_eastmoney = True
        return result

    summary = run_supervised_backfill(
        symbols=symbols,
        run_symbol=lambda symbol: run_batch([symbol]),
        run_batch=run_batch,
        batch_size=args.batch_size,
        progress_path=progress_path,
        max_consecutive_failures=args.max_consecutive_failures,
        sleep_seconds=args.sleep_seconds,
    )
    return summary


def main() -> None:
    summary = run_backfill_from_args()
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
