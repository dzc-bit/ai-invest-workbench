from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from astock_backtester.data.astock_adapter import AStockDataAdapter  # noqa: E402
from astock_backtester.data.providers import ADataProvider  # noqa: E402
from astock_backtester.data.symbols import normalize_symbol  # noqa: E402
from astock_backtester.data.warehouse import Warehouse  # noqa: E402


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def unique_symbols(symbols: list[object]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for symbol in symbols:
        code = normalize_symbol(symbol)
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def read_symbols_csv(path: Path) -> list[str]:
    frame = pd.read_csv(path, dtype=str)
    code_column = next(
        (column for column in ["stock_code", "code", "symbol"] if column in frame.columns),
        frame.columns[0],
    )
    return unique_symbols(frame[code_column].dropna().tolist())


def read_adata_local_symbols() -> list[str]:
    from adata.stock.cache import get_code_csv_path

    return read_symbols_csv(Path(get_code_csv_path()))


def write_symbols_cache(path: Path, symbols: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"symbol": symbols}).to_csv(path, index=False)


def load_symbols(cache_dir: Path, provider: ADataProvider, refresh: bool = False) -> tuple[list[str], str]:
    symbols_path = cache_dir / "symbols.csv"
    if symbols_path.exists() and not refresh:
        cached = read_symbols_csv(symbols_path)
        if cached:
            return cached, "cache"

    errors: list[str] = []
    try:
        symbols = unique_symbols(provider.list_symbols())
        if symbols:
            write_symbols_cache(symbols_path, symbols)
            return symbols, "provider"
    except Exception as exc:
        errors.append(f"provider: {exc}")

    try:
        symbols = unique_symbols(read_adata_local_symbols())
        if symbols:
            write_symbols_cache(symbols_path, symbols)
            return symbols, "adata-local"
    except Exception as exc:
        errors.append(f"adata-local: {exc}")

    if symbols_path.exists():
        cached = read_symbols_csv(symbols_path)
        if cached:
            return cached, "cache-after-error"

    raise RuntimeError("Unable to load symbols: " + "; ".join(errors))


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
        if event.get("status") == "completed" and event.get("symbol") and int(event.get("rows") or 0) > 0:
            completed.add(str(event["symbol"]))
    return completed


def fetch_daily_bars(symbol: str, start_date: str, end_date: str, adata_provider: ADataProvider) -> tuple[pd.DataFrame, str]:
    frame = adata_provider.fetch_daily_bars(symbol, start_date, end_date)
    if not frame.empty:
        return frame, "adata"

    frame = AStockDataAdapter.from_http_sources().fetch_daily_bars([symbol], start_date, end_date)
    if not frame.empty:
        return frame, "http"

    return pd.DataFrame(), "empty"


def fetch_daily_bars_from_source(
    symbol: str,
    start_date: str,
    end_date: str,
    source: str,
    adata_provider: ADataProvider,
) -> tuple[pd.DataFrame, str]:
    if source == "http":
        frame = AStockDataAdapter.from_http_sources().fetch_daily_bars([symbol], start_date, end_date)
        return frame, "http" if not frame.empty else "empty"
    if source == "adata":
        frame = adata_provider.fetch_daily_bars(symbol, start_date, end_date)
        return frame, "adata" if not frame.empty else "empty"

    frame = AStockDataAdapter.from_http_sources().fetch_daily_bars([symbol], start_date, end_date)
    if not frame.empty:
        return frame, "http"
    frame = adata_provider.fetch_daily_bars(symbol, start_date, end_date)
    if not frame.empty:
        return frame, "adata"
    return pd.DataFrame(), "empty"


def fetch_symbol(payload: tuple[str, str, str, str]) -> tuple[str, pd.DataFrame, str, float, str | None]:
    symbol, start_date, end_date, source = payload
    started = time.time()
    try:
        frame, resolved_source = fetch_daily_bars_from_source(
            symbol,
            start_date,
            end_date,
            source,
            ADataProvider(),
        )
        return symbol, frame, resolved_source, round(time.time() - started, 3), None
    except Exception as exc:
        return symbol, pd.DataFrame(), source, round(time.time() - started, 3), str(exc)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--start-date", default="2015-01-01")
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--refresh-symbols", action="store_true")
    parser.add_argument("--source", choices=["auto", "adata", "http"], default="http")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--write-batch-size", type=int, default=25)
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    progress_path = cache_dir / "import-progress.jsonl"
    warehouse = Warehouse(cache_dir)
    provider = ADataProvider()
    symbols, symbols_source = load_symbols(cache_dir, provider, refresh=args.refresh_symbols)
    if args.limit > 0:
        symbols = symbols[: args.limit]

    completed = load_completed_symbols(progress_path) if args.resume else set()

    append_jsonl(
        progress_path,
        {
            "event": "start",
            "time": datetime.now().isoformat(timespec="seconds"),
            "start_date": args.start_date,
            "end_date": args.end_date,
            "total_symbols": len(symbols),
            "symbols_source": symbols_source,
            "resume_completed": len(completed),
        },
    )

    imported_rows = 0
    failed = 0
    empty = 0
    pending_symbols = [(index, symbol) for index, symbol in enumerate(symbols, start=1) if symbol not in completed]
    by_symbol_index = {symbol: index for index, symbol in pending_symbols}
    batch: list[tuple[str, pd.DataFrame, str, float, int, int]] = []

    def flush_batch() -> None:
        nonlocal imported_rows
        if not batch:
            return
        warehouse.write_daily_bars(pd.concat([item[1] for item in batch], ignore_index=True))
        for symbol, _frame, source, seconds, rows, index in batch:
            imported_rows += rows
            append_jsonl(
                progress_path,
                {
                    "status": "completed",
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "symbol": symbol,
                    "index": index,
                    "total_symbols": len(symbols),
                    "source": source,
                    "rows": rows,
                    "imported_rows": imported_rows,
                    "seconds": seconds,
                },
            )
        batch.clear()

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [
            executor.submit(fetch_symbol, (symbol, args.start_date, args.end_date, args.source))
            for _, symbol in pending_symbols
        ]
        for future in as_completed(futures):
            symbol, frame, source, seconds, error = future.result()
            index = by_symbol_index[symbol]
            if error:
                failed += 1
                append_jsonl(
                    progress_path,
                    {
                        "status": "failed",
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "symbol": symbol,
                        "index": index,
                        "total_symbols": len(symbols),
                        "source": source,
                        "error": error,
                        "failed": failed,
                        "seconds": seconds,
                    },
                )
                continue

            rows = int(len(frame))
            if rows:
                batch.append((symbol, frame, source, seconds, rows, index))
            else:
                empty += 1
                append_jsonl(
                    progress_path,
                    {
                        "status": "completed",
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "symbol": symbol,
                        "index": index,
                        "total_symbols": len(symbols),
                        "source": source,
                        "rows": 0,
                        "imported_rows": imported_rows,
                        "seconds": seconds,
                    },
                )
            if len(batch) >= args.write_batch_size:
                flush_batch()
    flush_batch()

    if empty:
        append_jsonl(
            progress_path,
            {
                "event": "empty_rows_seen",
                "time": datetime.now().isoformat(timespec="seconds"),
                "empty_completed_records": empty,
            },
        )

    append_jsonl(
        progress_path,
        {
            "event": "finish",
            "time": datetime.now().isoformat(timespec="seconds"),
            "total_symbols": len(symbols),
            "imported_rows": imported_rows,
            "failed": failed,
        },
    )


if __name__ == "__main__":
    main()
