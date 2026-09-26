from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from threading import Lock, Thread
from typing import Any, Literal
from uuid import uuid4

import numpy as np
import pandas as pd

from astock_backtester.data.cache import LocalCache
from astock_backtester.data.capital_flow_crawler import diagnostics_should_skip_eastmoney
from astock_backtester.data.filelock import FileLockTimeout
from astock_backtester.data.importer import normalize_daily_bars
from astock_backtester.data.operations import (
    effective_a_share_date_range,
    fetch_capital_flow_into_cache,
)
from astock_backtester.data.trading_calendar import a_share_trade_dates
from astock_backtester.data.warehouse import Warehouse, classify_market_days_by_cross_section, lifecycle_bound
from astock_backtester.models import SyncJobStatus

OHLC_COLUMNS = ["open", "high", "low", "close"]

# 停牌分类的横截面样本下限：窗口交易日太少时“当日行数 ≥ 中位数×0.5”不稳，
# 退回旧行为（整窗必需、照抓不误），宁可多抓也不误判成停牌。
MIN_SUSPENSION_CLASSIFICATION_DAYS = 5

# 写批落盘的 FileLockTimeout 有界重试：1 次首发 + 2 次短退避，仍失败才上抛。
FULL_MARKET_WRITE_LOCK_ATTEMPTS = 3
FULL_MARKET_WRITE_LOCK_BACKOFF_SECONDS = 0.5

# 抓取窗口收窄时给缺失段两端各留的自然日缓冲：让首尾缺失日也能拿到前收盘，
# 并容忍缺口边缘的停牌/节假日抖动。窗口本身仍被夹在任务 [start, end] 内。
MISSING_FETCH_BUFFER_DAYS = 15


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


def _market_normal_days(window_frame: pd.DataFrame, required_dates: set[pd.Timestamp]) -> set[pd.Timestamp]:
    """窗口横截面分类出的“市场正常日”；样本不足或无分类时返回空集合（退回旧口径）。

    与 ``Warehouse.coverage()`` 共用 :func:`classify_market_days_by_cross_section`
    的阈值口径：当日全市场 OHLC 完整行数 ≥ 中位数 × 0.5 → 市场正常日，它的
    缺行是停牌类（不可补）；行数异常低 → thin day（疑似写入失败，可补）。
    """
    if len(required_dates) < MIN_SUSPENSION_CLASSIFICATION_DAYS:
        return set()
    rows_by_date = window_frame.groupby("_td_norm").size().to_dict()
    if not rows_by_date:
        return set()
    return classify_market_days_by_cross_section(rows_by_date).market_normal_days


def _suspension_exempt_required_dates(
    required_dates: set[pd.Timestamp],
    symbol: str,
    row_dates_by_symbol: dict[str, set[pd.Timestamp]],
    market_normal_days: set[pd.Timestamp],
) -> set[pd.Timestamp]:
    """把“窗口内已有行、却缺在市场正常日”的交易日按停牌类缺行剔除。

    三条例外与 ``Warehouse.coverage()`` 的 ``missing_rows``/``suspension_rows``
    口径一致（AGENTS §9）：
    - 落在 thin day 的缺行仍必需（可行动缺口，照抓）；
    - 落在市场正常日、但该股当天本就有行（缺的是市值字段而非行）仍必需；
    - 该股窗口内零行 → 返回原集合（整窗必需，保持旧口径照抓）。
    """
    if not market_normal_days:
        return required_dates
    row_dates = row_dates_by_symbol.get(symbol)
    if not row_dates:
        return required_dates
    return {day for day in required_dates if day not in market_normal_days or day in row_dates}


def _narrow_fetch_window(
    start_date: str,
    end_date: str,
    missing_dates: list[str] | None,
) -> tuple[str, str]:
    """把单票抓取窗口收窄到「实际缺失日 ± 缓冲」，两端夹回任务窗口。

    一轮补齐确认过哪些日子缺失后，下一轮只抓那段，而不是把整个
    ``[start_date, end_date]`` 再重抓一遍（旧行为在 ``float_market_cap``
    长期补不上的股票上是每轮 ~1 小时的全量重抓）。缺失集合为空（该股已被
    判完整、或快照里没有它的可行动缺口）时保持原窗口，语义与旧实现一致。
    """
    if not missing_dates:
        return start_date, end_date
    try:
        first = min(missing_dates)
        last = max(missing_dates)
        window_start = (
            datetime.strptime(first, "%Y-%m-%d") - timedelta(days=MISSING_FETCH_BUFFER_DAYS)
        ).strftime("%Y-%m-%d")
        window_end = (datetime.strptime(last, "%Y-%m-%d") + timedelta(days=MISSING_FETCH_BUFFER_DAYS)).strftime(
            "%Y-%m-%d"
        )
    except ValueError:
        return start_date, end_date
    window_start = max(window_start, start_date)
    window_end = min(window_end, end_date)
    if window_start > window_end:
        return start_date, end_date
    return window_start, window_end


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
    # 每股在窗口内**实际缺失**的可行动交易日（ISO 字符串，升序），用于把下一轮
    # 抓取窗口收窄到缺口附近（见 _narrow_fetch_window）。只收“部分缺失”的股票：
    # 窗口内零行、或整个必需集合全缺的股票不入表——调用方对缺席者回退整窗抓取，
    # 与旧行为完全一致，同时避免为它们复制几万条日期字符串。
    missing_dates_by_symbol: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class FilledMissingRows:
    daily_rows: int = 0
    market_cap_rows: int = 0

    @property
    def total(self) -> int:
        return self.daily_rows + self.market_cap_rows


FetchOutcomeKind = Literal["skipped", "empty", "rows", "error"]


@dataclass
class FetchOutcome:
    """单票抓取结果：``error`` 是抛异常，``empty`` 是 provider 正常返回空。"""

    symbol: str
    kind: FetchOutcomeKind
    frame: pd.DataFrame | None = None
    error: str | None = None


class FullMarketProgressSink:
    """全市场循环的进度写入目标（同步=本地 status，异步=job store）。

    两版循环共用一份核心实现，差异全部收敛在这里：同步版不写 store、不做
    取消检查；异步版每次进度都经锁落 store，供 HTTP 轮询读取。
    """

    concurrent: bool = False

    def snapshot(self) -> SyncJobStatus | None:
        raise NotImplementedError

    def mutate(self, **updates: object) -> None:
        raise NotImplementedError

    def append_failure(self, symbol: str, message: str) -> None:
        raise NotImplementedError

    def finish_cancelled(self) -> bool:
        return False


class LocalStatusSink(FullMarketProgressSink):
    def __init__(self, status: SyncJobStatus) -> None:
        self._status = status

    def snapshot(self) -> SyncJobStatus | None:
        return self._status

    def mutate(self, **updates: object) -> None:
        for key, value in updates.items():
            setattr(self._status, key, value)

    def append_failure(self, symbol: str, message: str) -> None:
        error = f"{symbol}: {message}"
        self._status.errors.append(error)
        self._status.last_error = error
        self._status.recent_failures = [
            *self._status.recent_failures,
            {"symbol": symbol, "reason": message},
        ][-20:]


class JobStatusSink(FullMarketProgressSink):
    concurrent = True

    def __init__(self, manager: SyncJobManager, job_id: str) -> None:
        self._manager = manager
        self._job_id = job_id

    def snapshot(self) -> SyncJobStatus | None:
        return self._manager.get_job(self._job_id)

    def mutate(self, **updates: object) -> None:
        self._manager._mutate(self._job_id, **updates)

    def append_failure(self, symbol: str, message: str) -> None:
        self._manager._append_failure(self._job_id, symbol, message)

    def finish_cancelled(self) -> bool:
        return self._manager._finish_cancelled(self._job_id)


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
        self._run_full_market_loop(
            list(symbols),
            effective_start_date,
            effective_end_date,
            snapshot,
            LocalStatusSink(status),
        )
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
            self._run_full_market_loop(symbols, start_date, end_date, snapshot, JobStatusSink(self, job_id))
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

    def _run_full_market_loop(
        self,
        symbols: list[str],
        start_date: str,
        end_date: str,
        snapshot: DailyCompletenessSnapshot,
        sink: FullMarketProgressSink,
    ) -> None:
        """全市场同步的唯一核心循环：同步 ``run_full_market`` 与异步作业共用。

        ``sink`` 决定进度写到哪、是否并发抓取（同步版顺序抓取、不写 store、不做
        取消检查；异步版分批 + 线程池 + 取消检查）。抓取结果处理、攒批落盘与
        ``filled_missing_rows`` 统计两版完全同源：

        - provider **正常返回空**（未抛异常）→ ``skipped_symbols``，不计失败、
          不写 ``errors``、不影响最终 status；
        - **抛异常** → ``failed_symbols`` + ``errors``，该批已成功行先落盘；
        - 攒批只在「行数达 ``full_market_write_batch_rows``」或「批内有失败」时
          落盘，写成功之后才清空待写帧（写失败不清，配合有界锁重试不丢批）；
        - 抓取窗口按快照里的每股缺口收窄（``_narrow_fetch_window``）：一轮补齐
          确认过缺失日后，下一轮只抓缺口 ± 缓冲，不再整窗重抓；完成判定与
          ``filled_missing_rows`` 统计仍以任务开始时的整窗快照为准，口径不变。
        """
        pending_frames: list[pd.DataFrame] = []
        pending_rows = 0
        workers = max(1, self.full_market_workers) if sink.concurrent else 1
        for batch in _chunks(symbols, self.full_market_batch_size):
            if sink.finish_cancelled():
                self._flush_full_market_frames(pending_frames)
                return
            batch_failed = False
            for outcome in self._iter_full_market_batch(batch, start_date, end_date, snapshot, workers):
                current = sink.snapshot()
                if current is not None:
                    sink.mutate(current_symbol=outcome.symbol, processed_symbols=current.processed_symbols + 1)
                if outcome.kind == "error":
                    batch_failed = True
                    if current is not None:
                        sink.mutate(failed_symbols=current.failed_symbols + 1)
                        sink.append_failure(outcome.symbol, outcome.error or "provider request failed")
                    continue
                frame = outcome.frame
                if outcome.kind == "skipped" or frame is None or frame.empty:
                    if current is not None:
                        sink.mutate(skipped_symbols=current.skipped_symbols + 1)
                    continue
                rows = int(len(frame))
                filled = self._count_full_market_filled_missing_rows(frame, start_date, end_date, snapshot)
                if current is not None:
                    sink.mutate(
                        completed_symbols=current.completed_symbols + 1,
                        returned_rows=current.returned_rows + rows,
                        imported_rows=current.imported_rows + rows,
                        filled_missing_rows=current.filled_missing_rows + filled.total,
                        filled_daily_rows=current.filled_daily_rows + filled.daily_rows,
                        filled_market_cap_rows=current.filled_market_cap_rows + filled.market_cap_rows,
                    )
                # 攒批落盘：write_daily_bars 每批都要「读整个分区 → 合并 → 整文件
                # 重写」，逐只写是 O(n²)。攒到阈值再写，把分区重写次数降到 1/批。
                pending_frames.append(frame)
                pending_rows += rows
                if pending_rows >= self.full_market_write_batch_rows:
                    self._flush_full_market_frames(pending_frames)
                    pending_rows = 0
            if batch_failed and pending_frames:
                self._flush_full_market_frames(pending_frames)
                pending_rows = 0
            if sink.finish_cancelled():
                self._flush_full_market_frames(pending_frames)
                return
        self._flush_full_market_frames(pending_frames)

    def _iter_full_market_batch(
        self,
        batch: list[str],
        start_date: str,
        end_date: str,
        snapshot: DailyCompletenessSnapshot,
        workers: int,
    ) -> Iterator[FetchOutcome]:
        """按批次抓取并按完成顺序产出结果；``workers <= 1`` 时顺序执行保持调用次序。"""
        if workers <= 1:
            for symbol in batch:
                yield self._fetch_daily_bars_outcome(symbol, start_date, end_date, snapshot)
            return
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self._fetch_daily_bars_outcome, symbol, start_date, end_date, snapshot): symbol
                for symbol in batch
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    yield future.result()
                except Exception as exc:  # noqa: BLE001 - 单票异常不得中断整批
                    yield FetchOutcome(symbol=symbol, kind="error", error=str(exc))

    def _fetch_daily_bars_outcome(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        snapshot: DailyCompletenessSnapshot,
    ) -> FetchOutcome:
        if symbol in snapshot.complete_symbols:
            return FetchOutcome(symbol=symbol, kind="skipped")
        # 窗口收窄：只抓该股在快照里实际缺失的那段（±缓冲），而不是整窗重抓。
        # 缺失集合缺席（零行/全缺/未入表）时回退整窗，与旧实现同一抓取范围。
        fetch_start, fetch_end = _narrow_fetch_window(
            start_date,
            end_date,
            snapshot.missing_dates_by_symbol.get(str(symbol)),
        )
        try:
            frame = self.provider.fetch_daily_bars(symbol, fetch_start, fetch_end)
        except Exception as exc:  # noqa: BLE001 - 异常是唯一算失败的路径
            return FetchOutcome(symbol=symbol, kind="error", error=str(exc))
        if frame is None or frame.empty:
            return FetchOutcome(symbol=symbol, kind="empty")
        return FetchOutcome(symbol=symbol, kind="rows", frame=frame)

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

        # 停牌分类（AGENTS §9）：直接用内存里的窗口帧算每交易日行数，横截面
        # 分出“市场正常日 / thin day”，口径与 Warehouse.coverage() 的
        # missing_rows/suspension_rows 一致；样本不足或无分类时返回空集合，
        # 退回旧口径（整窗必需、照抓不误）。
        market_normal_days = _market_normal_days(deduped, required_dates)
        row_dates_by_symbol: dict[str, set[pd.Timestamp]] = {}
        if market_normal_days:
            row_dates_by_symbol = deduped.groupby("symbol")["_td_norm"].apply(set).to_dict()

        complete: set[str] = set()
        missing_dates_by_symbol: dict[str, list[str]] = {}
        if "float_market_cap" in normalized.columns:
            cap_complete = normalized.dropna(subset=["float_market_cap"])
            actual_by_sym = cap_complete.groupby("symbol")["_td_norm"].apply(set)
            # 扫描集合 = 有市值行的股票 ∪ 窗口内有 OHLC 行的股票：后者覆盖“整只股票
            # 市值全空”的情况，它们同样要算出缺哪些日子用于收窄窗口。完成判定仍只
            # 对“有市值行”的股票做 issubset（与旧实现同一口径，不新增 complete 成员）。
            row_symbols = set(deduped["symbol"].unique())
            for symbol in set(actual_by_sym.index) | row_symbols:
                symbol_text = str(symbol)
                actual_dates = actual_by_sym.get(symbol, set())
                symbol_required = _lifecycle_clipped_required_dates(
                    required_dates, lifecycle.get(symbol_text), expected_dates[0], expected_dates[1]
                )
                if symbol_required is None:
                    continue
                symbol_required = _suspension_exempt_required_dates(
                    symbol_required, symbol_text, row_dates_by_symbol, market_normal_days
                )
                if symbol in actual_by_sym.index and symbol_required.issubset(actual_dates):
                    complete.add(symbol_text)
                missing = symbol_required - actual_dates
                # 只收“部分缺失”：全缺（含窗口内零行）回退整窗抓取，语义等价于旧实现。
                if missing and len(missing) < len(symbol_required):
                    missing_dates_by_symbol[symbol_text] = sorted(day.strftime("%Y-%m-%d") for day in missing)
        return DailyCompletenessSnapshot(
            complete_symbols=complete,
            existing_pairs=existing_pairs,
            existing_ohlc_complete=existing_ohlc_complete,
            existing_cap_null=existing_cap_null,
            missing_dates_by_symbol=missing_dates_by_symbol,
        )

    def _flush_full_market_frames(self, frames: list[pd.DataFrame]) -> int:
        """把攒批帧落盘并清空待写列表，返回写入行数。

        写成功**之后**才 ``frames.clear()``：旧实现先清后写，``FileLockTimeout``
        会把整批待写数据丢掉并让整单任务 failed。锁超时做有界短退避重试
        （见 :meth:`_write_daily_bars_with_lock_retry`），仍失败才上抛，此时
        frames 原样保留、数据不丢。
        """
        if not frames:
            return 0
        merged = pd.concat(frames, ignore_index=True)
        self._write_daily_bars_with_lock_retry(merged)
        frames.clear()
        return int(len(merged))

    def _write_daily_bars_with_lock_retry(self, frame: pd.DataFrame) -> None:
        for attempt in range(FULL_MARKET_WRITE_LOCK_ATTEMPTS):
            try:
                self.warehouse.write_daily_bars(frame)
                return
            except FileLockTimeout:
                if attempt >= FULL_MARKET_WRITE_LOCK_ATTEMPTS - 1:
                    raise
                time.sleep(FULL_MARKET_WRITE_LOCK_BACKOFF_SECONDS * (attempt + 1))

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
                    if diagnostics_should_skip_eastmoney(result.diagnostics):
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
