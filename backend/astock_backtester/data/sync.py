from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from threading import Lock, Thread
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd

from astock_backtester.data.cache import LocalCache
from astock_backtester.data.importer import normalize_daily_bars
from astock_backtester.data.operations import (
    effective_a_share_date_range,
    fetch_capital_flow_into_cache,
)
from astock_backtester.data.trading_calendar import a_share_trade_dates
from astock_backtester.data.warehouse import Warehouse, lifecycle_bound
from astock_backtester.models import SyncJobStatus

OHLC_COLUMNS = ["open", "high", "low", "close"]


def _lifecycle_clipped_required_dates(
    required_dates: set[pd.Timestamp],
    record: dict[str, str | None] | None,
    range_start: str,
    range_end: str,
) -> set[pd.Timestamp] | None:
    """Clip the sync window's required trading dates to a symbol's lifecycle.

    Returns ``None`` when the symbol's lifecycle window does not intersect the
    sync window at all (nothing is required, nothing can be complete), or when
    no lifecycle record exists — in which case the caller should use the full
    ``required_dates`` set.
    """
    listing = lifecycle_bound(record, "listing_date")
    delisted = lifecycle_bound(record, "delisted_date")
    if record is None or (listing is None and delisted is None):
        return required_dates
    window_start = pd.Timestamp(range_start)
    window_end = pd.Timestamp(range_end)
    clipped_start = max(window_start, listing) if listing is not None else window_start
    clipped_end = min(window_end, delisted) if delisted is not None else window_end
    if clipped_start > clipped_end:
        return set()
    if clipped_start <= window_start and clipped_end >= window_end:
        return required_dates
    return {date for date in required_dates if clipped_start <= date <= clipped_end}


@dataclass
class DailyCompletenessSnapshot:
    complete_symbols: set[str]
    # 只保留 (symbol, trade_date) 对的轻量索引与两个布尔向量：fill 统计只需要
    # “这个对在不在、OHLC 是否完整、市值是否缺失”，不需要整行数据。旧实现
    # 存整行 MultiIndex DataFrame，且每次 flush 用 iterrows 重建几十万到上千万
    # 对的 dict——这是全市场同步内存爆炸（10.7GB）与速率慢 8 倍的共同根因。
    existing_pairs: pd.MultiIndex
    existing_ohlc_complete: np.ndarray  # bool，按 existing_pairs 顺序对齐
    existing_cap_null: np.ndarray  # bool，按 existing_pairs 顺序对齐


@dataclass
class FilledMissingRows:
    daily_rows: int = 0
    market_cap_rows: int = 0

    @property
    def total(self) -> int:
        return self.daily_rows + self.market_cap_rows


@dataclass
class SyncJobManager:
    warehouse: Warehouse
    provider: object
    cache: LocalCache | None = None
    capital_flow_fetcher: Callable[[list[str], str, str], dict[str, Any]] | None = None
    full_market_batch_size: int = 100
    full_market_workers: int = 4
    full_market_write_batch_rows: int = 25_000
    capital_flow_batch_size: int = 50

    def __post_init__(self) -> None:
        self._jobs: dict[str, SyncJobStatus] = {}
        self._cancelled: set[str] = set()
        self._lock = Lock()

    def run_full_market(self, symbols: list[str], start_date: str, end_date: str) -> SyncJobStatus:
        effective_range = effective_a_share_date_range(start_date, end_date)
        effective_start_date, effective_end_date = effective_range or (start_date, end_date)
        status = SyncJobStatus(
            job_id=str(uuid4()),
            mode="full_market_bootstrap",
            status="running",
            total_symbols=len(symbols),
            start_date=date.fromisoformat(effective_start_date),
            end_date=date.fromisoformat(effective_end_date),
        )
        if effective_range is None:
            status.processed_symbols = len(symbols)
            status.skipped_symbols = len(symbols)
            status.status = "completed"
            return status
        snapshot = self._daily_completeness_snapshot(effective_start_date, effective_end_date)
        pending_frames: list[pd.DataFrame] = []
        pending_rows = 0
        for symbol in symbols:
            status.current_symbol = symbol
            status.processed_symbols += 1
            if symbol in snapshot.complete_symbols:
                status.skipped_symbols += 1
                continue
            try:
                frame = self.provider.fetch_daily_bars(symbol, effective_start_date, effective_end_date)
                if not frame.empty:
                    status.returned_rows += int(len(frame))
                    filled = self._count_full_market_filled_missing_rows(
                        frame,
                        effective_start_date,
                        effective_end_date,
                        snapshot,
                    )
                    status.filled_missing_rows += filled.total
                    status.filled_daily_rows += filled.daily_rows
                    status.filled_market_cap_rows += filled.market_cap_rows
                    # 攒批落盘：write_daily_bars 每批都要「读整个分区 → 合并 →
                    # 整文件重写」，逐只写是 O(n²)。攒到阈值再写，把每只股票
                    # 触发的分区重写次数从 1 降到 1/批。
                    pending_frames.append(frame)
                    pending_rows += int(len(frame))
                    if pending_rows >= self.full_market_write_batch_rows:
                        self.warehouse.write_daily_bars(
                            pd.concat(pending_frames, ignore_index=True)
                        )
                        pending_frames = []
                        pending_rows = 0
                    status.imported_rows += int(len(frame))
                    status.completed_symbols += 1
                else:
                    status.failed_symbols += 1
                    status.errors.append(f"{symbol}: provider returned no daily rows")
            except Exception as exc:
                status.failed_symbols += 1
                status.errors.append(f"{symbol}: {exc}")
        if pending_frames:
            self.warehouse.write_daily_bars(pd.concat(pending_frames, ignore_index=True))
        status.current_symbol = None
        status.status = "completed_with_errors" if status.failed_symbols else "completed"
        return status

    def start_full_market(self, symbols: list[str], start_date: str, end_date: str) -> SyncJobStatus:
        effective_range = effective_a_share_date_range(start_date, end_date)
        effective_start_date, effective_end_date = effective_range or (start_date, end_date)
        status = SyncJobStatus(
            job_id=str(uuid4()),
            mode="full_market_bootstrap",
            status="running",
            total_symbols=len(symbols),
            start_date=date.fromisoformat(effective_start_date),
            end_date=date.fromisoformat(effective_end_date),
        )
        self._store(status)
        if effective_range is None:
            status.status = "completed"
            status.processed_symbols = len(symbols)
            status.skipped_symbols = len(symbols)
            self._store(status)
            return self.get_job(status.job_id) or status
        thread = Thread(
            target=self._run_full_market_job,
            args=(status.job_id, list(symbols), effective_start_date, effective_end_date),
            daemon=True,
        )
        thread.start()
        return self.get_job(status.job_id) or status

    def start_capital_flow_backfill(self, symbols: list[str], start_date: str, end_date: str) -> SyncJobStatus:
        effective_range = effective_a_share_date_range(start_date, end_date)
        effective_start_date, effective_end_date = effective_range or (start_date, end_date)
        status = SyncJobStatus(
            job_id=str(uuid4()),
            mode="capital_flow_backfill",
            status="running",
            total_symbols=len(symbols),
            start_date=date.fromisoformat(effective_start_date),
            end_date=date.fromisoformat(effective_end_date),
        )
        self._store(status)
        if effective_range is None:
            status.status = "completed"
            status.processed_symbols = len(symbols)
            status.skipped_symbols = len(symbols)
            self._store(status)
            return self.get_job(status.job_id) or status
        thread = Thread(
            target=self._run_capital_flow_job,
            args=(status.job_id, list(symbols), effective_start_date, effective_end_date),
            daemon=True,
        )
        thread.start()
        return self.get_job(status.job_id) or status

    def get_job(self, job_id: str) -> SyncJobStatus | None:
        with self._lock:
            status = self._jobs.get(job_id)
            return status.model_copy(deep=True) if status else None

    def cancel_job(self, job_id: str) -> SyncJobStatus | None:
        with self._lock:
            status = self._jobs.get(job_id)
            if status is None:
                return None
            if status.status == "running":
                status.status = "cancelling"
                self._cancelled.add(job_id)
                self._jobs[job_id] = status
            return status.model_copy(deep=True)

    def _store(self, status: SyncJobStatus) -> None:
        with self._lock:
            self._jobs[status.job_id] = status.model_copy(deep=True)

    def _mutate(self, job_id: str, **updates: object) -> None:
        with self._lock:
            status = self._jobs[job_id]
            for key, value in updates.items():
                setattr(status, key, value)
            self._jobs[job_id] = status

    def _append_error(self, job_id: str, message: str) -> None:
        with self._lock:
            status = self._jobs[job_id]
            status.errors.append(message)
            status.last_error = message
            status.recent_failures = [*status.recent_failures, {"message": message}][-20:]
            self._jobs[job_id] = status

    def _is_cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancelled

    def _finish_cancelled(self, job_id: str) -> bool:
        if not self._is_cancel_requested(job_id):
            return False
        current = self.get_job(job_id)
        if current:
            current.current_symbol = None
            current.status = "cancelled"
            self._store(current)
        return True

    def _run_full_market_job(self, job_id: str, symbols: list[str], start_date: str, end_date: str) -> None:
        try:
            snapshot = self._daily_completeness_snapshot(start_date, end_date)
            pending_frames: list[pd.DataFrame] = []
            pending_rows = 0
            for batch in _chunks(symbols, self.full_market_batch_size):
                if self._finish_cancelled(job_id):
                    return
                frames: list[pd.DataFrame] = []
                batch_failed = False
                with ThreadPoolExecutor(max_workers=max(1, self.full_market_workers)) as executor:
                    futures = {
                        executor.submit(self._fetch_daily_bars_if_needed, symbol, start_date, end_date, snapshot.complete_symbols): symbol
                        for symbol in batch
                    }
                    for future in as_completed(futures):
                        symbol = futures[future]
                        self._mutate(job_id, current_symbol=symbol)
                        current = self.get_job(job_id)
                        if current:
                            self._mutate(job_id, processed_symbols=current.processed_symbols + 1)
                        try:
                            outcome, frame = future.result()
                        except Exception as exc:
                            batch_failed = True
                            current = self.get_job(job_id)
                            if current:
                                self._mutate(job_id, failed_symbols=current.failed_symbols + 1)
                            self._append_failure(job_id, symbol, str(exc))
                            continue
                        if outcome == "skipped":
                            current = self.get_job(job_id)
                            if current:
                                self._mutate(job_id, skipped_symbols=current.skipped_symbols + 1)
                            continue
                        if frame is None:
                            batch_failed = True
                            current = self.get_job(job_id)
                            if current:
                                self._mutate(job_id, failed_symbols=current.failed_symbols + 1)
                            self._append_failure(job_id, symbol, "provider returned no daily rows")
                            continue
                        if frame.empty:
                            batch_failed = True
                            current = self.get_job(job_id)
                            if current:
                                self._mutate(job_id, failed_symbols=current.failed_symbols + 1)
                            self._append_failure(job_id, symbol, "provider returned no daily rows")
                            continue
                        frames.append(frame)
                        current = self.get_job(job_id)
                        if current:
                            self._mutate(job_id, completed_symbols=current.completed_symbols + 1)
                if frames:
                    pending_frames.extend(frames)
                    pending_rows += sum(int(len(frame)) for frame in frames)
                    if batch_failed or pending_rows >= self.full_market_write_batch_rows or len(batch) >= self.full_market_batch_size:
                        pending_rows = self._flush_full_market_frames(job_id, pending_frames, snapshot)
                if self._finish_cancelled(job_id):
                    if pending_frames:
                        self._flush_full_market_frames(job_id, pending_frames, snapshot)
                    return
            if pending_frames:
                self._flush_full_market_frames(job_id, pending_frames, snapshot)
            final = self.get_job(job_id)
            if final:
                final.current_symbol = None
                final.status = "completed_with_errors" if final.failed_symbols else "completed"
                self._store(final)
        except Exception as exc:
            current = self.get_job(job_id)
            if current:
                current.current_symbol = None
                current.status = "failed"
                current.errors.append(str(exc))
                self._store(current)

    def _fetch_daily_bars_if_needed(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        complete_symbols: set[str],
    ) -> tuple[str, pd.DataFrame | None]:
        if symbol in complete_symbols:
            return "skipped", None
        return "fetched", self.provider.fetch_daily_bars(symbol, start_date, end_date)

    def _complete_daily_symbols(self, start_date: str, end_date: str) -> set[str]:
        return self._daily_completeness_snapshot(start_date, end_date).complete_symbols

    def incomplete_symbols(self, start_date: str, end_date: str) -> list[str]:
        """缺口基准补齐入口：窗口内“不完整”的股票名单。

        与 ``run_full_market`` 的跳过判定同源（同一份完整性快照），供外部
        补齐脚本/调用方只对真正有缺口的股票发起抓取，而不是全量扫描。
        退市股票从名单中剔除（与全市场同步的股票池规则一致）。
        """
        complete = self._daily_completeness_snapshot(start_date, end_date).complete_symbols
        pool = self.warehouse.read_daily_symbols(require_ohlc=True)
        try:
            delisted = self.warehouse.read_delisted_symbols()
        except Exception:  # noqa: BLE001 - 生命周期读取失败不阻塞补齐
            delisted = set()
        return sorted(symbol for symbol in pool if symbol not in complete and symbol not in delisted)

    def _daily_completeness_snapshot(self, start_date: str, end_date: str) -> DailyCompletenessSnapshot:
        expected_dates = effective_a_share_date_range(start_date, end_date)
        if expected_dates is None:
            return DailyCompletenessSnapshot(
                complete_symbols=set(),
                existing_pairs=pd.MultiIndex.from_arrays([np.array([], dtype=object), np.array([], dtype="datetime64[ns]")]),
                existing_ohlc_complete=np.zeros(0, dtype=bool),
                existing_cap_null=np.zeros(0, dtype=bool),
            )
        frame = self.warehouse.read_daily_bars(
            start_date=expected_dates[0],
            end_date=expected_dates[1],
            require_ohlc=True,
        )
        if frame.empty or not {"symbol", "trade_date"}.issubset(frame.columns):
            return DailyCompletenessSnapshot(
                complete_symbols=set(),
                existing_pairs=pd.MultiIndex.from_arrays([np.array([], dtype=object), np.array([], dtype="datetime64[ns]")]),
                existing_ohlc_complete=np.zeros(0, dtype=bool),
                existing_cap_null=np.zeros(0, dtype=bool),
            )
        required_dates = a_share_trade_dates(expected_dates[0], expected_dates[1])
        normalized = frame.copy()
        normalized["symbol"] = normalized["symbol"].astype(str)
        normalized["trade_date"] = pd.to_datetime(normalized["trade_date"], errors="coerce")
        normalized = normalized.dropna(subset=["symbol", "trade_date"])
        if normalized.empty:
            return DailyCompletenessSnapshot(
                complete_symbols=set(),
                existing_pairs=pd.MultiIndex.from_arrays([np.array([], dtype=object), np.array([], dtype="datetime64[ns]")]),
                existing_ohlc_complete=np.zeros(0, dtype=bool),
                existing_cap_null=np.zeros(0, dtype=bool),
            )
        normalized["_td_norm"] = normalized["trade_date"].dt.normalize()
        # Always build the pair index (needed for filled-missing-rows counting).
        # 向量化快照：不再保留整行 DataFrame，也不在 flush 时重建 pair dict。
        deduped = normalized.drop_duplicates(["symbol", "_td_norm"], keep="last")
        existing_pairs = pd.MultiIndex.from_arrays(
            [deduped["symbol"].astype(str).to_numpy(), deduped["_td_norm"].to_numpy()],
            names=["symbol", "trade_date"],
        )
        if all(column in deduped.columns for column in OHLC_COLUMNS):
            existing_ohlc_complete = deduped[OHLC_COLUMNS].notna().all(axis=1).to_numpy(dtype=bool)
        else:
            existing_ohlc_complete = np.zeros(len(deduped), dtype=bool)
        if "float_market_cap" in deduped.columns:
            existing_cap_null = deduped["float_market_cap"].isna().to_numpy(dtype=bool)
        else:
            existing_cap_null = np.ones(len(deduped), dtype=bool)

        lifecycle: dict[str, dict[str, str | None]] = {}
        try:
            lifecycle = self.warehouse.read_symbol_lifecycle()
        except Exception:
            lifecycle = {}

        complete: set[str] = set()
        if "float_market_cap" in normalized.columns:
            cap_complete = normalized.dropna(subset=["float_market_cap"])
            actual_by_sym = cap_complete.groupby("symbol")["_td_norm"].apply(set)
            for symbol, actual_dates in actual_by_sym.items():
                symbol_required = _lifecycle_clipped_required_dates(
                    required_dates, lifecycle.get(str(symbol)), expected_dates[0], expected_dates[1]
                )
                if symbol_required is not None and symbol_required.issubset(actual_dates):
                    complete.add(str(symbol))
        return DailyCompletenessSnapshot(
            complete_symbols=complete,
            existing_pairs=existing_pairs,
            existing_ohlc_complete=existing_ohlc_complete,
            existing_cap_null=existing_cap_null,
        )

    def _flush_full_market_frames(
        self,
        job_id: str,
        frames: list[pd.DataFrame],
        snapshot: DailyCompletenessSnapshot | None = None,
    ) -> int:
        if not frames:
            return 0
        merged = pd.concat(frames, ignore_index=True)
        frames.clear()
        returned_rows = int(len(merged))
        current = self.get_job(job_id)
        filled = self._count_full_market_filled_missing_rows(
            merged,
            current.start_date.isoformat() if current else None,
            current.end_date.isoformat() if current else None,
            snapshot,
        )
        self.warehouse.write_daily_bars(merged)
        current = self.get_job(job_id)
        if current:
            self._mutate(
                job_id,
                imported_rows=current.imported_rows + returned_rows,
                returned_rows=current.returned_rows + returned_rows,
                filled_missing_rows=current.filled_missing_rows + filled.total,
                filled_daily_rows=current.filled_daily_rows + filled.daily_rows,
                filled_market_cap_rows=current.filled_market_cap_rows + filled.market_cap_rows,
            )
        return 0

    def _count_full_market_filled_missing_rows(
        self,
        frame: pd.DataFrame,
        start_date: str | None,
        end_date: str | None,
        snapshot: DailyCompletenessSnapshot | None = None,
    ) -> FilledMissingRows:
        if frame.empty:
            return FilledMissingRows()
        normalized = normalize_daily_bars(frame)
        if normalized.empty or not {"symbol", "trade_date"}.issubset(normalized.columns):
            return FilledMissingRows()
        normalized["trade_date"] = pd.to_datetime(normalized["trade_date"], errors="coerce")
        normalized = normalized.dropna(subset=["symbol", "trade_date"])
        if start_date:
            normalized = normalized[normalized["trade_date"] >= pd.Timestamp(start_date)]
        if end_date:
            normalized = normalized[normalized["trade_date"] <= pd.Timestamp(end_date)]
        normalized = normalized.drop_duplicates(["symbol", "trade_date"], keep="last")
        if normalized.empty:
            return FilledMissingRows()
        if snapshot is None:
            snapshot = self._daily_completeness_snapshot(start_date, end_date) if start_date and end_date else None

        # 向量化统计（语义与逐行版本一致）：
        # - 新增行（快照里没有该 (symbol, date) 的完整 OHLC 行）且新行 OHLC 完整 → 日线补缺；
        # - 已有行 OHLC 完整但市值缺失、新行带市值 → 市值补缺。
        # 输入已按 (symbol, trade_date) 去重，逐对只出现一次，无顺序依赖。
        has_new_ohlc = (
            normalized[OHLC_COLUMNS].notna().all(axis=1).to_numpy(dtype=bool)
            if all(column in normalized.columns for column in OHLC_COLUMNS)
            else np.zeros(len(normalized), dtype=bool)
        )
        if "float_market_cap" in normalized.columns:
            new_cap_present = normalized["float_market_cap"].notna().to_numpy(dtype=bool)
        else:
            new_cap_present = np.zeros(len(normalized), dtype=bool)

        filled = FilledMissingRows()
        if snapshot is None or len(snapshot.existing_pairs) == 0:
            filled.daily_rows += int(has_new_ohlc.sum())
            return filled
        new_pairs = pd.MultiIndex.from_arrays(
            [normalized["symbol"].astype(str).to_numpy(), normalized["trade_date"].dt.normalize().to_numpy()],
            names=["symbol", "trade_date"],
        )
        position = snapshot.existing_pairs.get_indexer(new_pairs)
        present = position >= 0
        filled.daily_rows += int(np.logical_and(~present, has_new_ohlc).sum())
        if present.any():
            located = position[present]
            was_ohlc_complete = snapshot.existing_ohlc_complete[located]
            was_cap_null = snapshot.existing_cap_null[located]
            filled.market_cap_rows += int(
                np.logical_and(np.logical_and(was_ohlc_complete, was_cap_null), new_cap_present[present]).sum()
            )
        return filled

    def _run_capital_flow_job(self, job_id: str, symbols: list[str], start_date: str, end_date: str) -> None:
        try:
            if self.cache is None or self.capital_flow_fetcher is None:
                raise RuntimeError("capital-flow job is not configured")
            skip_eastmoney = False
            for batch in _chunks(symbols, self.capital_flow_batch_size):
                if self._finish_cancelled(job_id):
                    return
                current_symbol = batch[0] if len(batch) == 1 else f"{batch[0]}..{batch[-1]}"
                self._mutate(job_id, current_symbol=current_symbol)
                try:
                    result = fetch_capital_flow_into_cache(
                        cache=self.cache,
                        warehouse=self.warehouse,
                        # Bind the current skip flag eagerly so the closure is
                        # not affected by later batch iterations (B023).
                        capital_flow_fetcher=lambda requested, start, end, _skip=skip_eastmoney: _call_capital_flow_fetcher(
                            self.capital_flow_fetcher,
                            list(requested),
                            start,
                            end,
                            skip_eastmoney=_skip,
                        ),
                        symbols=batch,
                        start_date=start_date,
                        end_date=end_date,
                        refresh_coverage=False,
                    )
                    if _diagnostics_should_skip_eastmoney(result.diagnostics):
                        skip_eastmoney = True
                    current = self.get_job(job_id)
                    if not current:
                        continue
                    failure_reasons = _capital_flow_failure_reasons(batch, result)
                    completed_symbols = _completed_capital_flow_symbols(batch, result, failure_reasons)
                    updates = {
                        "processed_symbols": current.processed_symbols + len(batch),
                        "completed_symbols": current.completed_symbols + len(completed_symbols),
                        "failed_symbols": current.failed_symbols + len(failure_reasons),
                        "skipped_symbols": current.skipped_symbols + len(result.skipped_symbols),
                        "imported_rows": current.imported_rows + result.imported_rows,
                        "returned_rows": current.returned_rows + result.returned_rows,
                    }
                    self._mutate(job_id, **updates)
                    for symbol, reason in failure_reasons.items():
                        self._append_failure(job_id, symbol, reason)
                except Exception as exc:
                    current = self.get_job(job_id)
                    if current:
                        self._mutate(
                            job_id,
                            processed_symbols=current.processed_symbols + len(batch),
                            failed_symbols=current.failed_symbols + len(batch),
                        )
                    self._append_error(job_id, f"{current_symbol}: {exc}")
                if self._finish_cancelled(job_id):
                    return
            final = self.get_job(job_id)
            if final:
                final.current_symbol = None
                final.status = "completed_with_errors" if final.failed_symbols else "completed"
                self._store(final)
        except Exception as exc:
            current = self.get_job(job_id)
            if current:
                current.current_symbol = None
                current.status = "failed"
                current.errors.append(str(exc))
                self._store(current)

    def _append_failure(self, job_id: str, symbol: str, message: str) -> None:
        with self._lock:
            status = self._jobs[job_id]
            error = f"{symbol}: {message}"
            status.errors.append(error)
            status.last_error = error
            status.recent_failures = [
                *status.recent_failures,
                {"symbol": symbol, "reason": message},
            ][-20:]
            self._jobs[job_id] = status


def _call_capital_flow_fetcher(
    fetcher: Callable[[list[str], str, str], dict[str, Any]],
    symbols: list[str],
    start_date: str,
    end_date: str,
    *,
    skip_eastmoney: bool,
) -> dict[str, Any]:
    try:
        return fetcher(symbols, start_date, end_date, skip_eastmoney=skip_eastmoney)  # type: ignore[misc]
    except TypeError as exc:
        if "skip_eastmoney" not in str(exc):
            raise
        return fetcher(symbols, start_date, end_date)


def _diagnostics_should_skip_eastmoney(diagnostics: list[dict[str, Any]]) -> bool:
    return any(
        item.get("code") == "provider_attempt_failed"
        and item.get("provider") == "eastmoney"
        and item.get("error_code") == "network_error"
        for item in diagnostics
    )


def _capital_flow_failure_reasons(
    batch: list[str],
    result: Any,
) -> dict[str, str]:
    selected = {str(symbol) for symbol in batch}
    reasons: dict[str, str] = {}
    for failure in getattr(result, "failures", []):
        if not isinstance(failure, dict):
            continue
        symbol = failure.get("symbol")
        if not symbol or str(symbol) not in selected:
            continue
        reasons.setdefault(str(symbol), _failure_message(failure))
    for diagnostic in getattr(result, "diagnostics", []):
        if not isinstance(diagnostic, dict) or diagnostic.get("code") != "date_coverage_shortfall":
            continue
        symbol = diagnostic.get("symbol")
        if not symbol or str(symbol) not in selected:
            continue
        reasons.setdefault(str(symbol), _failure_message(diagnostic))
    for symbol in getattr(result, "missing_symbols", []):
        symbol_text = str(symbol)
        if symbol_text in selected:
            reasons.setdefault(symbol_text, "capital-flow rows missing for requested range")
    return reasons


def _completed_capital_flow_symbols(
    batch: list[str],
    result: Any,
    failure_reasons: dict[str, str],
) -> list[str]:
    selected = {str(symbol) for symbol in batch}
    completed = {
        str(symbol)
        for symbol in [*getattr(result, "fetched_symbols", []), *getattr(result, "skipped_symbols", [])]
        if str(symbol) in selected
    }
    if not completed and not failure_reasons and _diagnostics_include_not_needed(getattr(result, "diagnostics", [])):
        completed = selected
    return sorted(symbol for symbol in completed if symbol not in failure_reasons)


def _diagnostics_include_not_needed(diagnostics: list[dict[str, Any]]) -> bool:
    return any(isinstance(item, dict) and item.get("code") == "capital_flow_backfill_not_needed" for item in diagnostics)


def _failure_message(item: dict[str, Any]) -> str:
    return str(item.get("error") or item.get("message") or item.get("code") or "capital-flow request failed")


def _chunks(items: list[str], size: int) -> list[list[str]]:
    chunk_size = max(1, size)
    return [items[index : index + chunk_size] for index in range(0, len(items), chunk_size)]


def _failure_symbols(failures: list[dict[str, Any]]) -> set[str]:
    return {
        str(item.get("symbol"))
        for item in failures
        if isinstance(item, dict) and item.get("symbol")
    }
