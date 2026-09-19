from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

import pandas as pd

from astock_backtester.data.cache import LocalCache
from astock_backtester.data.importer import normalize_daily_bars
from astock_backtester.data.trading_calendar import a_share_trade_dates
from astock_backtester.data.warehouse import (
    KNOWN_CAPITAL_FLOW_LISTING_LAG_DAYS,
    KNOWN_CAPITAL_FLOW_SOURCE_GAP_DATES,
    Warehouse,
    lifecycle_bound,
    uses_symbol_capital_flow_source_start,
)
from astock_backtester.models import (
    DailyBarsCoverageItem,
    DailyBarsCoverageResponse,
    DataOperationResult,
    DatasetCoverage,
    ServiceHealth,
    ServiceLogEntry,
)

DailyBarsFetcher = Callable[[Sequence[str], str, str], pd.DataFrame]
CapitalFlowFetcher = Callable[[Sequence[str], str, str], dict[str, Any]]
logger = logging.getLogger(__name__)


def _date_range(start_date: pd.Timestamp, end_date: pd.Timestamp) -> set[pd.Timestamp]:
    return a_share_trade_dates(start_date, end_date)


def effective_a_share_date_range(start_date: str, end_date: str) -> tuple[str, str] | None:
    trade_dates = sorted(a_share_trade_dates(pd.Timestamp(start_date), pd.Timestamp(end_date)))
    if not trade_dates:
        return None
    return trade_dates[0].date().isoformat(), trade_dates[-1].date().isoformat()


def build_daily_bars_coverage(
    cache: LocalCache,
    warehouse: Warehouse | None = None,
    symbols: Sequence[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> DailyBarsCoverageResponse:
    requested_start_date = pd.Timestamp(start_date) if start_date else None
    requested_end_date = pd.Timestamp(end_date) if end_date else None
    bars = pd.DataFrame()
    used_warehouse = False
    if warehouse is not None:
        try:
            bars = warehouse.read_daily_bars(symbols=symbols, start_date=start_date, end_date=end_date)
            used_warehouse = not bars.empty
        except Exception as exc:
            logger.warning(
                "warehouse daily-bars read failed; falling back to cache: %s",
                exc,
                exc_info=True,
            )
            bars = pd.DataFrame()
            used_warehouse = False
    if bars.empty:
        bars = cache.read_daily_bars()
    if bars.empty:
        return DailyBarsCoverageResponse(items=[])

    if not used_warehouse and symbols:
        selected_symbols = {str(symbol).strip() for symbol in symbols if str(symbol).strip()}
        if selected_symbols:
            bars = bars.loc[bars["symbol"].astype(str).isin(selected_symbols)]
    if not used_warehouse and start_date:
        bars = bars.loc[bars["trade_date"] >= pd.Timestamp(start_date)]
    if not used_warehouse and end_date:
        bars = bars.loc[bars["trade_date"] <= pd.Timestamp(end_date)]
    if bars.empty:
        return DailyBarsCoverageResponse(items=[])

    items = []
    lifecycle_records: dict[str, dict[str, str | None]] = {}
    if warehouse is not None:
        try:
            lifecycle_records = warehouse.read_symbol_lifecycle(
                [str(symbol) for symbol in bars["symbol"].dropna().astype(str).unique()]
            )
        except Exception as exc:
            logger.warning("symbol lifecycle read failed; falling back to unclipped coverage: %s", exc)
            lifecycle_records = {}
    # 未显式指定 end_date 时，覆盖窗口的终点应取“仓库全局最新数据日”，而不是该股
    # 自己的最后一行——否则一只停在 7 月的股票在逐股表里永远显示 missing=0，与
    # Warehouse.coverage() 的累计缺口口径矛盾（覆盖表说没缺、同步却要补）。
    derived_window_end = requested_end_date
    if derived_window_end is None and warehouse is not None and used_warehouse:
        try:
            for item in warehouse.coverage():
                if item.dataset == "daily_bars" and item.end_date is not None:
                    derived_window_end = pd.Timestamp(item.end_date)
                    break
        except Exception as exc:  # noqa: BLE001 - 损坏分区不应让整个覆盖端点失败
            logger.warning(
                "warehouse coverage scan failed; per-symbol coverage falls back to its own last row: %s", exc
            )
    for symbol, frame in bars.groupby("symbol", sort=True):
        frame = frame.sort_values("trade_date")
        data_start_date = frame["trade_date"].min()
        data_end_date = frame["trade_date"].max()
        coverage_start_date = requested_start_date if requested_start_date is not None else data_start_date
        if requested_end_date is not None:
            coverage_end_date = requested_end_date
        elif derived_window_end is not None and derived_window_end > data_end_date:
            coverage_end_date = derived_window_end
        else:
            coverage_end_date = data_end_date
        present_dates = set(frame["trade_date"])
        record = lifecycle_records.get(str(symbol))
        listing_date = lifecycle_bound(record, "listing_date")
        delisted_date = lifecycle_bound(record, "delisted_date")
        if listing_date is not None and listing_date > coverage_start_date:
            coverage_start_date = listing_date
        if delisted_date is not None and delisted_date < coverage_end_date:
            coverage_end_date = delisted_date
        if coverage_start_date <= coverage_end_date:
            expected_dates = _date_range(coverage_start_date, coverage_end_date)
        else:
            expected_dates = set()
        missing_trade_dates = sorted(expected_dates - present_dates)
        lifecycle_status = "unknown"
        if record is not None:
            lifecycle_status = "delisted" if delisted_date is not None else str(record.get("status") or "listed")
            if lifecycle_status not in ("unknown", "listed", "delisted"):
                lifecycle_status = "listed"
        items.append(
            DailyBarsCoverageItem(
                symbol=str(symbol),
                start_date=data_start_date.date(),
                end_date=data_end_date.date(),
                rows=int(len(frame)),
                missing_trade_dates=[item.date() for item in missing_trade_dates],
                missing_capital_flow_dates=[
                    item.date()
                    for item in frame.loc[frame["main_net_inflow"].isna(), "trade_date"].tolist()
                ],
                missing_market_cap_dates=[
                    item.date()
                    for item in frame.loc[frame["float_market_cap"].isna(), "trade_date"].tolist()
                ],
                listing_date=listing_date.date() if listing_date is not None else None,
                delisted_date=delisted_date.date() if delisted_date is not None else None,
                lifecycle_status=lifecycle_status,  # type: ignore[arg-type]
            )
        )
    return DailyBarsCoverageResponse(items=items)


def import_daily_bars_into_cache(
    cache: LocalCache,
    frame: pd.DataFrame,
    source: str,
    warehouse: Warehouse | None = None,
) -> DataOperationResult:
    cache.write_daily_bars(frame)
    if warehouse is not None:
        warehouse.write_daily_bars(frame)
    coverage = _safe_coverage(cache, warehouse)
    return DataOperationResult(
        status="ok",
        imported_rows=int(len(frame)),
        coverage=coverage,
        logs=[ServiceLogEntry(level="info", message=f"Imported daily bars from {source}")],
    )


def fetch_daily_bars_into_cache(
    cache: LocalCache,
    fetcher: DailyBarsFetcher,
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    warehouse: Warehouse | None = None,
    capital_flow_fetcher: CapitalFlowFetcher | None = None,
    refresh_coverage: bool = True,
) -> DataOperationResult:
    """``refresh_coverage=False`` 供 AI 写路径使用：``coverage()`` 是全仓
    60 秒级重扫，逐只循环补齐时 N 次调用就 N 次重扫；AI 侧改用
    ``DataServiceState.start_coverage_refresh`` 的后台刷新，这里不再同步算。"""
    requested_symbols = [str(symbol) for symbol in symbols]
    effective_range = effective_a_share_date_range(start_date, end_date)
    if effective_range is None:
        coverage = _safe_coverage(cache, warehouse) if refresh_coverage else []
        return DataOperationResult(
            status="ok",
            imported_rows=0,
            requested_symbols=requested_symbols,
            fetched_symbols=[],
            missing_symbols=[],
            skipped_symbols=requested_symbols,
            coverage=coverage,
            logs=[ServiceLogEntry(level="info", message="Requested date range contains no A-share trading days")],
            diagnostics=[
                {
                    "code": "no_a_share_trade_dates",
                    "source": "trading_calendar",
                    "start_date": start_date,
                    "end_date": end_date,
                }
            ],
        )
    effective_start_date, effective_end_date = effective_range
    frame = fetcher(requested_symbols, effective_start_date, effective_end_date)
    logs = [ServiceLogEntry(level="info", message=f"Fetched {len(frame)} daily bar rows")]
    diagnostics: list[dict[str, Any]] = []
    if (effective_start_date, effective_end_date) != (start_date, end_date):
        diagnostics.append(
            {
                "code": "a_share_trade_date_range_clipped",
                "source": "trading_calendar",
                "requested_start_date": start_date,
                "requested_end_date": end_date,
                "start_date": effective_start_date,
                "end_date": effective_end_date,
            }
        )
    failures: list[dict[str, Any]] = []
    capital_flow_missing_symbols: list[str] = []
    if not frame.empty and capital_flow_fetcher is not None:
        frame, merge_logs, merge_diagnostics, failures, _ = _merge_capital_flow_from_fetcher(
            frame=frame,
            fetcher=capital_flow_fetcher,
            requested_symbols=requested_symbols,
            start_date=effective_start_date,
            end_date=effective_end_date,
            only_missing=False,
        )
        logs.extend(merge_logs)
        diagnostics.extend(merge_diagnostics)
        capital_flow_missing_symbols = _symbols_with_missing_main_net_inflow(frame, requested_symbols)
        if capital_flow_missing_symbols:
            diagnostics.append(
                {
                    "code": "capital_flow_crawler_unfilled_main_net_inflow",
                    "source": "capital_flow_crawler",
                    "symbols": capital_flow_missing_symbols,
                    "start_date": effective_start_date,
                    "end_date": effective_end_date,
                    "message": "Capital-flow crawler did not fill main_net_inflow for all fetched daily-bar rows",
                }
            )
            logs.append(
                ServiceLogEntry(
                    level="warning",
                    message=f"Capital-flow crawler left missing main_net_inflow for symbols: {', '.join(capital_flow_missing_symbols)}",
                )
            )
    if frame.empty:
        fetched_symbols: list[str] = []
    else:
        cache.write_daily_bars(frame)
        if warehouse is not None:
            warehouse.write_daily_bars(frame)
            derived_listings = derive_listing_dates_from_frame(frame)
            if derived_listings:
                try:
                    warehouse.upsert_symbol_lifecycle(
                        [
                            {"symbol": symbol, "listing_date": listing_date, "status": "listed"}
                            for symbol, listing_date in derived_listings.items()
                        ]
                    )
                except Exception as exc:
                    logger.warning("symbol lifecycle upsert failed after daily-bars fetch: %s", exc)
        fetched_symbols = sorted(frame["symbol"].astype(str).unique().tolist())
    missing_symbols = sorted(
        {
            *(symbol for symbol in requested_symbols if symbol not in fetched_symbols),
            *capital_flow_missing_symbols,
        }
    )
    if capital_flow_fetcher is not None and frame.empty:
        diagnostics.append(
            {
                "code": "capital_flow_crawler_skipped",
                "reason": "no_daily_bar_rows",
                "source": "capital_flow_crawler",
            }
        )
    if missing_symbols:
        logs.append(ServiceLogEntry(level="warning", message=f"Missing symbols: {', '.join(missing_symbols)}"))
    if failures:
        failed_symbols = sorted({str(item.get("symbol", "")) for item in failures if item.get("symbol")})
        if failed_symbols:
            logs.append(
                ServiceLogEntry(
                    level="warning",
                    message=f"Capital-flow crawler failed for symbols: {', '.join(failed_symbols)}",
                )
            )
    coverage = _safe_coverage(cache, warehouse) if refresh_coverage else []
    return DataOperationResult(
        status="partial" if missing_symbols or failures else "ok",
        imported_rows=int(len(frame)),
        requested_symbols=requested_symbols,
        fetched_symbols=fetched_symbols,
        missing_symbols=missing_symbols,
        coverage=coverage,
        logs=logs,
        diagnostics=diagnostics,
        failures=failures,
    )


def fetch_capital_flow_into_cache(
    cache: LocalCache,
    capital_flow_fetcher: CapitalFlowFetcher,
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    warehouse: Warehouse | None = None,
    refresh_coverage: bool = True,
) -> DataOperationResult:
    requested_symbols = [str(symbol) for symbol in symbols]
    effective_range = effective_a_share_date_range(start_date, end_date)
    if effective_range is None:
        coverage = _safe_coverage(cache, warehouse) if refresh_coverage else []
        return DataOperationResult(
            status="ok",
            imported_rows=0,
            returned_rows=0,
            requested_symbols=requested_symbols,
            fetched_symbols=[],
            missing_symbols=[],
            skipped_symbols=requested_symbols,
            coverage=coverage,
            logs=[ServiceLogEntry(level="info", message="Requested date range contains no A-share trading days")],
            diagnostics=[
                {
                    "code": "no_a_share_trade_dates",
                    "source": "trading_calendar",
                    "start_date": start_date,
                    "end_date": end_date,
                }
            ],
        )
    effective_start_date, effective_end_date = effective_range
    frame = _read_existing_daily_bars(cache, warehouse, requested_symbols, effective_start_date, effective_end_date)
    logs: list[ServiceLogEntry] = []
    diagnostics: list[dict[str, Any]] = []
    if (effective_start_date, effective_end_date) != (start_date, end_date):
        diagnostics.append(
            {
                "code": "a_share_trade_date_range_clipped",
                "source": "trading_calendar",
                "requested_start_date": start_date,
                "requested_end_date": end_date,
                "start_date": effective_start_date,
                "end_date": effective_end_date,
            }
        )
    failures: list[dict[str, Any]] = []

    skipped_symbols = _symbols_with_complete_capital_flow(frame, requested_symbols, effective_start_date, effective_end_date)
    fetch_symbols = sorted(symbol for symbol in requested_symbols if symbol not in set(skipped_symbols))
    if not fetch_symbols:
        coverage = _safe_coverage(cache, warehouse) if refresh_coverage else []
        return DataOperationResult(
            status="ok",
            imported_rows=0,
            returned_rows=0,
            requested_symbols=requested_symbols,
            fetched_symbols=[],
            missing_symbols=[],
            skipped_symbols=skipped_symbols,
            coverage=coverage,
            logs=[
                ServiceLogEntry(level="info", message="Capital-flow coverage already complete for requested rows"),
            ],
            diagnostics=[
                *diagnostics,
                {
                    "code": "capital_flow_backfill_not_needed",
                    "source": "capital_flow_crawler",
                    "requested_symbols": len(requested_symbols),
                    "skipped_symbols": skipped_symbols,
                },
            ],
        )

    rows, fetch_logs, fetch_diagnostics, failures = _fetch_capital_flow_rows(
        fetcher=capital_flow_fetcher,
        requested_symbols=fetch_symbols,
        start_date=effective_start_date,
        end_date=effective_end_date,
    )
    logs.extend(fetch_logs)
    diagnostics.extend(fetch_diagnostics)
    if _diagnostics_include_not_needed(fetch_diagnostics) and not rows and not failures:
        coverage = _safe_coverage(cache, warehouse) if refresh_coverage else []
        all_skipped_symbols = sorted({*skipped_symbols, *fetch_symbols})
        return DataOperationResult(
            status="ok",
            imported_rows=0,
            returned_rows=0,
            requested_symbols=requested_symbols,
            fetched_symbols=[],
            missing_symbols=[],
            skipped_symbols=all_skipped_symbols,
            coverage=coverage,
            logs=[
                ServiceLogEntry(level="info", message="Capital-flow coverage already complete for requested rows"),
            ],
            diagnostics=[
                *diagnostics,
                {
                    "code": "capital_flow_backfill_not_needed",
                    "source": "capital_flow_crawler",
                    "requested_symbols": len(requested_symbols),
                    "skipped_symbols": all_skipped_symbols,
                },
            ],
        )
    merged_frame, merged_rows, existing_fetched_symbols = _merge_capital_flow_rows(
        frame,
        rows,
        only_missing=True,
    )
    existing_imported_by_symbol = _capital_flow_imported_rows_by_symbol(
        frame,
        merged_frame,
        only_missing=True,
    )
    standalone_frame = _standalone_daily_bars_from_capital_flow_rows(
        rows,
        fetch_symbols,
        start_date=effective_start_date,
        end_date=effective_end_date,
        existing_frame=frame,
    )
    standalone_rows = int(len(standalone_frame))
    standalone_symbols = (
        sorted(standalone_frame["symbol"].astype(str).unique().tolist())
        if not standalone_frame.empty
        else []
    )
    imported_rows = int(merged_rows + standalone_rows)
    returned_by_symbol = _capital_flow_returned_rows_by_symbol(rows)
    returned_symbols = sorted(symbol for symbol, count in returned_by_symbol.items() if count > 0)
    fetched_symbols = sorted({*existing_fetched_symbols, *standalone_symbols, *returned_symbols})
    standalone_imported_by_symbol = _frame_row_counts_by_symbol(standalone_frame)
    imported_by_symbol = _merge_symbol_counts(existing_imported_by_symbol, standalone_imported_by_symbol)
    incomplete_symbols = _symbols_with_remaining_existing_capital_flow_gap(
        merged_frame,
        fetch_symbols,
        diagnostics,
    )
    known_gap_symbols = _symbols_with_only_known_capital_flow_gaps(merged_frame, incomplete_symbols)
    if known_gap_symbols:
        diagnostics.extend(
            {
                "code": "capital_flow_known_source_gap_remaining",
                "source": "capital_flow_crawler",
                "symbol": symbol,
                "message": "Only known public-source capital-flow gap dates remain for this symbol.",
            }
            for symbol in known_gap_symbols
        )
        incomplete_symbols = [
            symbol for symbol in incomplete_symbols if symbol not in set(known_gap_symbols)
        ]
    logs.append(
        ServiceLogEntry(
            level="warning" if imported_rows == 0 else "info",
            message=f"Capital-flow crawler merged {imported_rows} rows as primary main_net_inflow source",
        )
    )
    diagnostics.append(
        {
            "code": "capital_flow_crawler_merge",
            "requested_symbols": len(fetch_symbols),
            "merged_rows": imported_rows,
            "source": "capital_flow_crawler",
        }
    )
    diagnostics.append(
        {
            "code": "capital_flow_crawler_fetch_summary",
            "source": "capital_flow_crawler",
            "requested_symbols": len(fetch_symbols),
            "processed_symbols": len(fetch_symbols),
            "returned_rows": len(rows),
            "imported_rows": imported_rows,
            "failed_symbols": sorted(
                {
                    str(item.get("symbol"))
                    for item in failures
                    if isinstance(item, dict) and item.get("symbol")
                }
            ),
            "skipped_symbols": skipped_symbols,
        }
    )
    diagnostics.extend(
        {
            "code": "capital_flow_symbol_summary",
            "source": "capital_flow_crawler",
            "symbol": symbol,
            "returned_rows": returned_by_symbol.get(symbol, 0),
            "imported_rows": imported_by_symbol.get(symbol, 0),
        }
        for symbol in fetch_symbols
    )
    failures.extend(_capital_flow_incomplete_failures(diagnostics, incomplete_symbols, failures))
    if standalone_rows > 0:
        diagnostics.append(
            {
                "code": "capital_flow_crawler_standalone_rows",
                "requested_symbols": len(fetch_symbols),
                "standalone_rows": standalone_rows,
                "source": "capital_flow_crawler",
                "message": (
                    "Capital-flow rows were written before daily OHLCV rows; "
                    "daily-bar coverage will remain incomplete until historical prices are fetched."
                ),
            }
        )
    if imported_rows == 0:
        diagnostics.append(
            {
                "code": "capital_flow_crawler_zero_merge",
                "requested_symbols": len(fetch_symbols),
                "source": "capital_flow_crawler",
                "message": "Capital-flow crawler returned no rows that could be merged into main_net_inflow",
            }
        )

    if imported_rows > 0:
        frames_to_write = [item for item in [merged_frame, standalone_frame] if not item.empty]
        write_frame = normalize_daily_bars(pd.concat(frames_to_write, ignore_index=True))
        cache.write_daily_bars(write_frame)
        if warehouse is not None:
            warehouse.write_daily_bars(write_frame)

    missing_symbols = sorted(
        {
            *(symbol for symbol in fetch_symbols if symbol not in fetched_symbols),
            *(
                str(item.get("symbol"))
                for item in failures
                if isinstance(item, dict) and item.get("symbol")
            ),
        }
    )
    if failures:
        failed_symbols = sorted({str(item.get("symbol", "")) for item in failures if item.get("symbol")})
        if failed_symbols:
            logs.append(
                ServiceLogEntry(
                    level="warning",
                    message=f"Capital-flow crawler failed for symbols: {', '.join(failed_symbols)}",
                )
            )
    if missing_symbols:
        logs.append(ServiceLogEntry(level="warning", message=f"Missing capital-flow symbols: {', '.join(missing_symbols)}"))
    coverage = _safe_coverage(cache, warehouse) if refresh_coverage else []
    return DataOperationResult(
        status="partial" if missing_symbols or failures else "ok",
        imported_rows=imported_rows,
        returned_rows=len(rows),
        requested_symbols=requested_symbols,
        fetched_symbols=fetched_symbols,
        missing_symbols=missing_symbols,
        skipped_symbols=skipped_symbols,
        coverage=coverage,
        logs=logs,
        diagnostics=diagnostics,
        failures=failures,
    )


def _fetch_capital_flow_rows(
    fetcher: CapitalFlowFetcher,
    requested_symbols: Sequence[str],
    start_date: str,
    end_date: str,
) -> tuple[list[dict[str, Any]], list[ServiceLogEntry], list[dict[str, Any]], list[dict[str, Any]]]:
    logs: list[ServiceLogEntry] = []
    diagnostics: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    try:
        result = fetcher(list(requested_symbols), start_date, end_date)
    except Exception as exc:
        failure = {"code": "capital_flow_crawler_error", "error": str(exc), "source": "capital_flow_crawler"}
        failures.append(failure)
        diagnostics.append({**failure, "message": str(exc)})
        logs.append(ServiceLogEntry(level="warning", message=f"Capital-flow crawler failed: {exc}"))
        return [], logs, diagnostics, failures

    rows = result.get("rows", []) if isinstance(result, dict) else []
    raw_failures = result.get("failures", []) if isinstance(result, dict) else []
    raw_diagnostics = result.get("diagnostics", []) if isinstance(result, dict) else []
    failures = [item for item in raw_failures if isinstance(item, dict)]
    diagnostics.extend(item for item in raw_diagnostics if isinstance(item, dict))
    return [item for item in rows if isinstance(item, dict)], logs, diagnostics, failures


def _capital_flow_incomplete_failures(
    diagnostics: Sequence[dict[str, Any]],
    requested_symbols: Sequence[str],
    existing_failures: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    selected = {str(symbol) for symbol in requested_symbols}
    failed = {str(item.get("symbol")) for item in existing_failures if isinstance(item, dict) and item.get("symbol")}
    incomplete: list[dict[str, Any]] = []
    for item in diagnostics:
        symbol = item.get("symbol")
        if item.get("code") != "date_coverage_shortfall" or not symbol:
            continue
        symbol_text = str(symbol)
        if symbol_text not in selected or symbol_text in failed:
            continue
        incomplete.append(
            {
                "symbol": symbol_text,
                "code": "date_coverage_shortfall",
                "error": f"date_coverage_shortfall: {item.get('message') or 'capital-flow date coverage is incomplete'}",
            }
        )
        failed.add(symbol_text)
    return incomplete


def _diagnostics_include_not_needed(diagnostics: Sequence[dict[str, Any]]) -> bool:
    return any(isinstance(item, dict) and item.get("code") == "capital_flow_backfill_not_needed" for item in diagnostics)


def _read_existing_daily_bars(
    cache: LocalCache,
    warehouse: Warehouse | None,
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    if warehouse is not None:
        try:
            warehouse_frame = warehouse.read_daily_bars(symbols=symbols, start_date=start_date, end_date=end_date)
            if not warehouse_frame.empty:
                frames.append(warehouse_frame)
        except Exception as exc:
            logger.warning(
                "warehouse daily-bars read failed; falling back to cache: %s",
                exc,
                exc_info=True,
            )
    cache_frame = cache.read_daily_bars()
    if not cache_frame.empty:
        selected = {str(symbol) for symbol in symbols}
        cache_frame = cache_frame.loc[cache_frame["symbol"].astype(str).isin(selected)]
        cache_frame = cache_frame.loc[cache_frame["trade_date"] >= pd.Timestamp(start_date)]
        cache_frame = cache_frame.loc[cache_frame["trade_date"] <= pd.Timestamp(end_date)]
        if not cache_frame.empty:
            frames.append(cache_frame)
    if not frames:
        return pd.DataFrame()
    frame = normalize_daily_bars(pd.concat(frames, ignore_index=True))
    frame = frame.drop_duplicates(["symbol", "trade_date"], keep="first")
    return frame.reset_index(drop=True)


def _symbols_with_complete_capital_flow(
    frame: pd.DataFrame,
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
) -> list[str]:
    if frame.empty or "symbol" not in frame or "trade_date" not in frame or "main_net_inflow" not in frame:
        return []
    expected_dates = _date_range(pd.Timestamp(start_date), pd.Timestamp(end_date))
    if not expected_dates:
        return []
    normalized = frame.copy()
    normalized["symbol"] = normalized["symbol"].astype(str)
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"], errors="coerce")
    normalized = normalized.dropna(subset=["symbol", "trade_date"])
    if normalized.empty:
        return []

    # Vectorized pre-computation: group by symbol and compute date sets in one pass
    normalized["_td_norm"] = normalized["trade_date"].dt.normalize()
    flow_mask = normalized["main_net_inflow"].notna()
    flow_dates_by_sym = normalized[flow_mask].groupby("symbol")["_td_norm"].apply(set).to_dict()
    first_daily_by_sym = normalized.groupby("symbol")["trade_date"].min().to_dict()

    base_expected = expected_dates - KNOWN_CAPITAL_FLOW_SOURCE_GAP_DATES
    symbol_set = {str(s) for s in symbols}
    complete: list[str] = []

    for symbol in symbol_set:
        flow_dates = flow_dates_by_sym.get(symbol)
        if flow_dates is None:
            continue
        first_daily_date = first_daily_by_sym.get(symbol)

        # Check special source-start adjustments (rare path)
        flow_start = min(flow_dates)
        warehouse_start = normalized["trade_date"].min()
        source_start_boundary = (
            pd.notna(first_daily_date)
            and pd.notna(warehouse_start)
            and (
                uses_symbol_capital_flow_source_start(
                    symbol,
                    pd.Timestamp(flow_start),
                    pd.Timestamp(first_daily_date),
                    pd.Timestamp(warehouse_start),
                )
                or _uses_listing_day_capital_flow_source_start(
                    normalized.loc[normalized["symbol"] == symbol], pd.Timestamp(flow_start)
                )
            )
        )
        if source_start_boundary:
            effective = {
                td for td in base_expected
                if not (pd.Timestamp(td) < pd.Timestamp(flow_start))
            }
            if effective.issubset(flow_dates):
                complete.append(symbol)
        else:
            if base_expected.issubset(flow_dates):
                complete.append(symbol)
    return sorted(complete)


def _uses_listing_day_capital_flow_source_start(data: pd.DataFrame, flow_start: pd.Timestamp) -> bool:
    if "listing_days" not in data or data.empty:
        return False
    first_daily_date = data["trade_date"].dropna().min()
    if pd.isna(first_daily_date):
        return False
    first_rows = data.loc[data["trade_date"] == first_daily_date]
    listing_days = pd.to_numeric(first_rows["listing_days"], errors="coerce").dropna()
    if listing_days.empty or listing_days.min() > 10:
        return False
    lag_days = (pd.Timestamp(flow_start) - pd.Timestamp(first_daily_date)).days
    return 0 <= lag_days <= KNOWN_CAPITAL_FLOW_LISTING_LAG_DAYS


def _merge_capital_flow_from_fetcher(
    frame: pd.DataFrame,
    fetcher: CapitalFlowFetcher,
    requested_symbols: Sequence[str],
    start_date: str,
    end_date: str,
    *,
    only_missing: bool,
) -> tuple[pd.DataFrame, list[ServiceLogEntry], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    logs: list[ServiceLogEntry] = []
    diagnostics: list[dict[str, Any]] = []
    rows, fetch_logs, fetch_diagnostics, failures = _fetch_capital_flow_rows(
        fetcher=fetcher,
        requested_symbols=requested_symbols,
        start_date=start_date,
        end_date=end_date,
    )
    logs.extend(fetch_logs)
    diagnostics.extend(fetch_diagnostics)
    merged_frame, merged_rows, fetched_symbols = _merge_capital_flow_rows(frame, rows, only_missing=only_missing)
    logs.append(
        ServiceLogEntry(
            level="warning" if merged_rows == 0 else "info",
            message=f"Capital-flow crawler merged {merged_rows} rows as primary main_net_inflow source",
        )
    )
    diagnostics.append(
        {
            "code": "capital_flow_crawler_merge",
            "requested_symbols": len(list(requested_symbols)),
            "merged_rows": merged_rows,
            "source": "capital_flow_crawler",
        }
    )
    if merged_rows == 0:
        diagnostics.append(
            {
                "code": "capital_flow_crawler_zero_merge",
                "requested_symbols": len(list(requested_symbols)),
                "source": "capital_flow_crawler",
                "message": "Capital-flow crawler returned no rows that could be merged into main_net_inflow",
            }
    )
    return merged_frame, logs, diagnostics, failures, fetched_symbols


def _standalone_daily_bars_from_capital_flow_rows(
    rows: Sequence[dict[str, Any]],
    symbols: Sequence[str],
    *,
    start_date: str,
    end_date: str,
    existing_frame: pd.DataFrame | None = None,
) -> pd.DataFrame:
    selected = {str(symbol) for symbol in symbols}
    if not rows or not selected:
        return pd.DataFrame()
    frame = pd.DataFrame(list(rows))
    required = {"symbol", "trade_date", "main_net_inflow"}
    if frame.empty or not required.issubset(frame.columns):
        return pd.DataFrame()
    frame["symbol"] = frame["symbol"].astype(str)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["main_net_inflow"] = pd.to_numeric(frame["main_net_inflow"], errors="coerce")
    frame = frame.loc[frame["symbol"].isin(selected)]
    frame = frame.loc[frame["trade_date"] >= pd.Timestamp(start_date)]
    frame = frame.loc[frame["trade_date"] <= pd.Timestamp(end_date)]
    if existing_frame is not None and not existing_frame.empty and {"symbol", "trade_date"}.issubset(existing_frame.columns):
        existing = existing_frame[["symbol", "trade_date"]].copy()
        existing["symbol"] = existing["symbol"].astype(str)
        existing["trade_date"] = pd.to_datetime(existing["trade_date"], errors="coerce")
        existing_pairs = set(zip(existing["symbol"], existing["trade_date"], strict=False))
        frame = frame.loc[
            [
                (symbol, trade_date) not in existing_pairs
                for symbol, trade_date in zip(frame["symbol"], frame["trade_date"], strict=False)
            ]
        ]
    frame = frame.dropna(subset=["trade_date", "main_net_inflow"])
    if frame.empty:
        return pd.DataFrame()
    out = frame[["symbol", "trade_date", "main_net_inflow"]].drop_duplicates(
        ["symbol", "trade_date"],
        keep="last",
    )
    out["open"] = float("nan")
    out["high"] = float("nan")
    out["low"] = float("nan")
    out["close"] = float("nan")
    out["volume"] = 0.0
    out["amount"] = 0.0
    out["change_pct"] = float("nan")
    out["change"] = float("nan")
    out["turnover_rate"] = float("nan")
    out["pre_close"] = float("nan")
    out["float_market_cap"] = float("nan")
    out["total_market_cap"] = float("nan")
    out["is_st"] = False
    out["is_suspended"] = False
    out["listing_days"] = 9999
    out["source"] = "capital-flow-crawler"
    return normalize_daily_bars(out)


def _frame_row_counts_by_symbol(frame: pd.DataFrame) -> dict[str, int]:
    if frame.empty or "symbol" not in frame:
        return {}
    return {
        str(symbol): int(count)
        for symbol, count in frame["symbol"].dropna().astype(str).value_counts().items()
    }


def _merge_symbol_counts(*items: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for item in items:
        for symbol, count in item.items():
            merged[symbol] = merged.get(symbol, 0) + int(count)
    return merged


def _capital_flow_returned_rows_by_symbol(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        trade_date = str(row.get("trade_date") or "").strip()
        if not symbol or not trade_date:
            continue
        key = (symbol, trade_date)
        if key in seen:
            continue
        seen.add(key)
        counts[symbol] = counts.get(symbol, 0) + 1
    return counts


def _capital_flow_imported_rows_by_symbol(
    before: pd.DataFrame,
    after: pd.DataFrame,
    *,
    only_missing: bool,
) -> dict[str, int]:
    if before.empty or after.empty:
        return {}
    left = normalize_daily_bars(before).set_index(["symbol", "trade_date"])
    right = normalize_daily_bars(after).set_index(["symbol", "trade_date"])
    common = left.index.intersection(right.index)
    if common.empty:
        return {}
    before_values = left.loc[common, "main_net_inflow"]
    after_values = right.loc[common, "main_net_inflow"]
    mask = before_values.isna() & after_values.notna() if only_missing else after_values.notna()
    target = common[mask.to_numpy()]
    counts: dict[str, int] = {}
    for symbol, _trade_date in target:
        symbol_text = str(symbol)
        counts[symbol_text] = counts.get(symbol_text, 0) + 1
    return counts


def _symbols_with_missing_main_net_inflow(frame: pd.DataFrame, symbols: Sequence[str]) -> list[str]:
    if frame.empty or "main_net_inflow" not in frame.columns:
        return sorted({str(symbol) for symbol in symbols})
    selected = {str(symbol) for symbol in symbols}
    data = frame.loc[frame["symbol"].astype(str).isin(selected)]
    if data.empty:
        return []
    missing = data.loc[data["main_net_inflow"].isna(), "symbol"]
    return sorted({str(symbol) for symbol in missing.tolist()})


def _symbols_with_remaining_existing_capital_flow_gap(
    frame: pd.DataFrame,
    symbols: Sequence[str],
    diagnostics: Sequence[dict[str, Any]],
) -> list[str]:
    provider_by_symbol = _capital_flow_provider_by_symbol(diagnostics)
    if frame.empty:
        return sorted(
            {
                str(item.get("symbol"))
                for item in diagnostics
                if isinstance(item, dict)
                and item.get("code") == "date_coverage_shortfall"
                and provider_by_symbol.get(str(item.get("symbol"))) != "sina"
                and item.get("symbol")
            }
        )
    missing_symbols = _symbols_with_missing_main_net_inflow(frame, symbols)
    return sorted(symbol for symbol in missing_symbols if provider_by_symbol.get(symbol) != "sina")


def _symbols_with_only_known_capital_flow_gaps(frame: pd.DataFrame, symbols: Sequence[str]) -> list[str]:
    if frame.empty or "main_net_inflow" not in frame or "trade_date" not in frame or "symbol" not in frame:
        return []
    known_dates = {pd.Timestamp(value) for value in KNOWN_CAPITAL_FLOW_SOURCE_GAP_DATES}
    out: list[str] = []
    normalized = frame.copy()
    normalized["symbol"] = normalized["symbol"].astype(str)
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"], errors="coerce")
    for symbol in symbols:
        missing_dates = set(
            normalized.loc[
                (normalized["symbol"] == str(symbol)) & normalized["main_net_inflow"].isna(),
                "trade_date",
            ].dropna()
        )
        if missing_dates and missing_dates.issubset(known_dates):
            out.append(str(symbol))
    return sorted(out)


def _capital_flow_provider_by_symbol(diagnostics: Sequence[dict[str, Any]]) -> dict[str, str]:
    providers: dict[str, str] = {}
    for item in diagnostics:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol")
        provider = item.get("provider")
        if not symbol or not provider:
            continue
        symbol_text = str(symbol)
        if item.get("code") == "provider_fallback_used":
            providers[symbol_text] = str(provider)
        else:
            providers.setdefault(symbol_text, str(provider))
    return providers


def _merge_capital_flow_rows(
    frame: pd.DataFrame,
    rows: Sequence[dict[str, Any]],
    *,
    only_missing: bool,
) -> tuple[pd.DataFrame, int, list[str]]:
    if frame.empty:
        return frame, 0, []
    out = normalize_daily_bars(frame)
    if not rows:
        return out, 0, []
    flow = pd.DataFrame(list(rows))
    if not {"symbol", "trade_date", "main_net_inflow"}.issubset(flow.columns):
        return out, 0, []
    flow = flow[["symbol", "trade_date", "main_net_inflow"]].copy()
    flow["symbol"] = flow["symbol"].astype(str)
    flow["trade_date"] = pd.to_datetime(flow["trade_date"], errors="coerce")
    flow["main_net_inflow"] = pd.to_numeric(flow["main_net_inflow"], errors="coerce")
    flow = flow.dropna(subset=["trade_date", "main_net_inflow"])
    if flow.empty:
        return out, 0, []

    out = out.set_index(["symbol", "trade_date"]).sort_index()
    flow = flow.drop_duplicates(["symbol", "trade_date"], keep="last").set_index(["symbol", "trade_date"]).sort_index()
    common_index = flow.index.intersection(out.index)
    if common_index.empty:
        return out.reset_index(), 0, []
    if only_missing:
        target_index = common_index[out.loc[common_index, "main_net_inflow"].isna().to_numpy()]
    else:
        target_index = common_index
    if target_index.empty:
        return out.reset_index(), 0, []
    out.loc[target_index, "main_net_inflow"] = flow.loc[target_index, "main_net_inflow"]
    fetched_symbols = sorted({str(symbol) for symbol, _date in target_index})
    return out.reset_index().sort_values(["symbol", "trade_date"]).reset_index(drop=True), int(len(target_index)), fetched_symbols


def _count_merged_main_net_inflow(before: pd.DataFrame, after: pd.DataFrame, *, only_missing: bool) -> int:
    if before.empty or after.empty:
        return 0
    left = normalize_daily_bars(before).set_index(["symbol", "trade_date"])
    right = normalize_daily_bars(after).set_index(["symbol", "trade_date"])
    common = left.index.intersection(right.index)
    if common.empty:
        return 0
    before_values = left.loc[common, "main_net_inflow"]
    after_values = right.loc[common, "main_net_inflow"]
    if only_missing:
        return int((before_values.isna() & after_values.notna()).sum())
    return int(after_values.notna().sum())


def _safe_coverage(cache: LocalCache, warehouse: Warehouse | None) -> list[DatasetCoverage]:
    if warehouse is not None:
        try:
            coverage = warehouse.coverage()
            if any(item.symbols > 0 for item in coverage):
                return coverage
        except Exception as exc:
            logger.warning(
                "warehouse coverage read failed; falling back to cache: %s",
                exc,
                exc_info=True,
            )
    try:
        return cache.coverage()
    except Exception as exc:
        logger.warning(
            "cache coverage read failed; returning an empty snapshot: %s",
            exc,
            exc_info=True,
        )
        return [
            DatasetCoverage(dataset="daily_bars", symbols=0, start_date=None, end_date=None),
            DatasetCoverage(dataset="market_cap", symbols=0, start_date=None, end_date=None),
            DatasetCoverage(dataset="capital_flow", symbols=0, start_date=None, end_date=None),
        ]


def build_service_health(
    cache: LocalCache,
    warehouse: Warehouse,
    port: int | None = None,
    *,
    process_id: int | None = None,
    executable_path: str | None = None,
    executable_sha256: str | None = None,
    started_at: datetime | str | None = None,
    instance_id: str | None = None,
) -> ServiceHealth:
    try:
        coverage = warehouse.coverage()
    except Exception as exc:
        logger.warning(
            "warehouse coverage read failed; falling back to cache: %s",
            exc,
            exc_info=True,
        )
        coverage = []
    if not any(item.symbols > 0 for item in coverage):
        try:
            coverage = cache.coverage()
        except Exception as exc:
            logger.warning(
                "cache coverage read failed; returning an empty snapshot: %s",
                exc,
                exc_info=True,
            )
            coverage = [
                DatasetCoverage(dataset="daily_bars", symbols=0, start_date=None, end_date=None),
                DatasetCoverage(dataset="market_cap", symbols=0, start_date=None, end_date=None),
                DatasetCoverage(dataset="capital_flow", symbols=0, start_date=None, end_date=None),
            ]
    return ServiceHealth(
        ok=True,
        cache_path=str(cache.root.resolve()),
        port=port,
        process_id=process_id,
        executable_path=executable_path,
        executable_sha256=executable_sha256,
        started_at=started_at,
        instance_id=instance_id,
        coverage=coverage,
    )


def derive_listing_dates_from_frame(frame: pd.DataFrame) -> dict[str, str]:
    """Derive ``symbol -> listing_date`` (ISO) from fetched daily-bar rows.

    Imported rows carry ``listing_days`` (9999 means unknown), so a single
    in-memory conversion recovers the listing date without extra network
    calls. Only rows with a plausible ``listing_days`` are used.
    """
    if frame.empty or "listing_days" not in frame.columns:
        return {}
    listing_frame = frame[["symbol", "trade_date", "listing_days"]].copy()
    listing_frame["listing_days"] = pd.to_numeric(listing_frame["listing_days"], errors="coerce")
    listing_frame = listing_frame.dropna(subset=["listing_days"])
    listing_frame = listing_frame.loc[(listing_frame["listing_days"] > 0) & (listing_frame["listing_days"] < 9999)]
    if listing_frame.empty:
        return {}
    listing_frame["trade_date"] = pd.to_datetime(listing_frame["trade_date"], errors="coerce")
    listing_frame = listing_frame.dropna(subset=["trade_date"])
    listing_frame["listing_date"] = listing_frame["trade_date"] - pd.to_timedelta(
        listing_frame["listing_days"].astype("int64"), unit="D"
    )
    listing_frame = listing_frame.sort_values(["symbol", "trade_date"])
    listing_frame = listing_frame.drop_duplicates("symbol", keep="last")
    return {
        str(row["symbol"]): row["listing_date"].date().isoformat()
        for _, row in listing_frame.iterrows()
    }


def refresh_symbol_lifecycle(
    warehouse: Warehouse,
    current_listings: dict[str, str | None],
    *,
    min_current_symbols: int = 1000,
    stale_delist_days: int = 30,
) -> dict[str, Any]:
    """Best-effort ``symbol_lifecycle`` refresh used around full-market syncs.

    ``current_listings`` maps every symbol the source currently reports to its
    listing date (``None`` when unknown). When the source list looks complete
    (at least ``min_current_symbols`` entries), warehouse symbols that are
    absent from it *and* have no recent OHLC rows are marked delisted with
    their last known trade date. A suspiciously small source list only
    refreshes listing dates and never delists anything, so a flaky upstream
    cannot silently freeze the sync universe.
    """
    if not current_listings:
        return {"status": "skipped", "reason": "empty_current_listings"}
    rows: list[dict[str, str | None]] = [
        {"symbol": symbol, "listing_date": listing_date, "status": "listed"}
        for symbol, listing_date in current_listings.items()
    ]
    delisted: list[dict[str, str | None]] = []
    if len(current_listings) >= min_current_symbols:
        try:
            warehouse_symbols = warehouse.read_daily_symbols(require_ohlc=True)
        except Exception:
            warehouse_symbols = []
        candidates = sorted(set(warehouse_symbols) - {str(symbol) for symbol in current_listings})
        if candidates:
            try:
                history = warehouse.read_daily_bars(
                    symbols=candidates,
                    start_date=(pd.Timestamp.today() - pd.Timedelta(days=stale_delist_days)).date().isoformat(),
                    require_ohlc=True,
                )
            except Exception:
                history = pd.DataFrame()
            recent_symbols = set() if history.empty else set(history["symbol"].astype(str).unique())
            stale_candidates = [symbol for symbol in candidates if symbol not in recent_symbols]
            if stale_candidates:
                try:
                    stale_history = warehouse.read_daily_bars(symbols=stale_candidates, require_ohlc=True)
                except Exception:
                    stale_history = pd.DataFrame()
                if not stale_history.empty:
                    stale_history = stale_history.copy()
                    stale_history["trade_date"] = pd.to_datetime(stale_history["trade_date"], errors="coerce")
                    last_dates = stale_history.groupby(stale_history["symbol"].astype(str))["trade_date"].max()
                else:
                    last_dates = pd.Series(dtype="datetime64[ns]")
                for symbol in stale_candidates:
                    last_date = last_dates.get(symbol)
                    delisted.append(
                        {
                            "symbol": symbol,
                            "delisted_date": last_date.date().isoformat() if pd.notna(last_date) else None,
                            "status": "delisted",
                        }
                    )
    written = warehouse.upsert_symbol_lifecycle([*rows, *delisted])
    return {
        "status": "ok",
        "listed_upserts": len(rows),
        "delisted_upserts": len(delisted),
        "written_rows": written,
    }
