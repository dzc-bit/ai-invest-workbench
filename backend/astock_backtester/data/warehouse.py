from __future__ import annotations

import bisect
import os
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from statistics import median

import pandas as pd
import pyarrow.parquet as pq

from astock_backtester.data.filelock import CrossProcessFileLock
from astock_backtester.data.importer import normalize_daily_bars
from astock_backtester.data.trading_calendar import a_share_trade_dates
from astock_backtester.models import DatasetCoverage

OHLC_COLUMNS = ["open", "high", "low", "close"]
GAP_PROFILE_TTL_SECONDS = 600.0
GAP_PROFILE_DEFAULT_PARTITION_YEARS = 2
GAP_PROFILE_TOP_STALE = 12
# 本地股票池计数的缓存 TTL：与缺口画像同一模式（TTL + 写入失效）。真实数据仓
# coverage() 全仓扫描实测 ~10s，而红绿家数 provider 循环的总预算只有 8s——
# 计数绝不能在行情链路上现算。
SYMBOL_COUNT_TTL_SECONDS = 600.0
KNOWN_CAPITAL_FLOW_SOURCE_GAP_DATES = {
    pd.Timestamp("2018-08-07"),
    pd.Timestamp("2019-04-04"),
    pd.Timestamp("2019-04-19"),
    pd.Timestamp("2022-12-14"),
    pd.Timestamp("2024-07-16"),
}
KNOWN_CAPITAL_FLOW_SOURCE_START_SYMBOL_PREFIXES = ("920",)
KNOWN_CAPITAL_FLOW_SOURCE_START_SYMBOLS = {"001872", "001914", "601360"}
KNOWN_CAPITAL_FLOW_SOURCE_START_DATES = {pd.Timestamp("2021-12-29")}
KNOWN_CAPITAL_FLOW_LISTING_LAG_DAYS = 90


class Warehouse:
    def __init__(self, cache_root: str | Path) -> None:
        self.cache_root = Path(cache_root)
        self.root = self.cache_root / "warehouse"
        self.daily_bars_root = self.root / "daily_bars"
        self.daily_bars_root.mkdir(parents=True, exist_ok=True)
        self.sqlite_path = self.root / "metadata.sqlite"
        self._init_db()
        self._gap_profile_lock = threading.Lock()
        self._gap_profile_cache: tuple[float, dict[str, object]] | None = None
        self._symbol_count_lock = threading.Lock()
        self._symbol_count_cache: tuple[float, int] | None = None
        self._corrupt_partitions_lock = threading.Lock()
        self._corrupt_partitions: dict[str, str] = {}

    def _init_db(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS datasets (
                    dataset TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS symbol_sync_state (
                    symbol TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    start_date TEXT,
                    end_date TEXT,
                    rows INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    provider TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS symbol_lifecycle (
                    symbol TEXT PRIMARY KEY,
                    listing_date TEXT,
                    delisted_date TEXT,
                    status TEXT NOT NULL DEFAULT 'listed',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def _partition_path(self, year: int) -> Path:
        return self.daily_bars_root / f"year={year}" / "daily_bars.parquet"

    def _partition_paths_for_range(self, start_date: str | None, end_date: str | None) -> list[Path]:
        paths = sorted(self.daily_bars_root.glob("year=*/daily_bars.parquet"))
        if not paths or (start_date is None and end_date is None):
            return paths

        start_year = date.min.year if start_date is None else pd.Timestamp(start_date).year
        end_year = date.max.year if end_date is None else pd.Timestamp(end_date).year
        return [
            path
            for path in paths
            if start_year <= int(path.parent.name.split("year=", 1)[1]) <= end_year
        ]

    def daily_bars_parquet_glob(self) -> str | None:
        """Hive-style glob for the daily-bars parquet partitions.

        Public read-only helper for the AI module's DuckDB query tool; returns
        ``None`` when no partition exists yet.
        """
        if not self._partition_paths_for_range(None, None):
            return None
        return str(self.daily_bars_root / "year=*" / "daily_bars.parquet")

    def daily_bars_parquet_paths(self) -> list[str]:
        """Explicit partition file paths (no glob) for sandboxed DuckDB views."""
        return [str(path) for path in self._partition_paths_for_range(None, None)]

    def write_daily_bars(self, frame: pd.DataFrame) -> None:
        normalized = normalize_daily_bars(frame)
        if normalized.empty:
            return
        normalized["year"] = normalized["trade_date"].dt.year
        for year, year_frame in normalized.groupby("year"):
            path = self._partition_path(int(year))
            path.parent.mkdir(parents=True, exist_ok=True)
            year_frame = year_frame.drop(columns=["year"])
            # 跨进程互斥：read-modify-write 必须原子化，否则桌面端 sidecar 与
            # 外部脚本并发写同一分区会丢失更新甚至撕裂文件（footer 不匹配）。
            with CrossProcessFileLock(path):
                if path.exists():
                    current = self._safe_read_parquet(path)
                    if not current.empty and {"symbol", "trade_date"}.issubset(current.columns):
                        year_frame = (
                            year_frame.set_index(["symbol", "trade_date"])
                            .combine_first(current.set_index(["symbol", "trade_date"]))
                            .reset_index()
                        )
                year_frame = year_frame.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
                self._atomic_write_parquet(year_frame, path)
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute("INSERT OR REPLACE INTO datasets(dataset) VALUES('daily_bars')")
        self.invalidate_gap_profile()

    @staticmethod
    def _atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
        """临时文件写完后 ``os.replace`` 原子替换。

        ``to_parquet`` 直接写目标路径时会先 truncate 再逐块写，任何并发读取者
        （包括桌面端自己读 coverage）都可能读到半成品并报
        ``Parquet magic bytes not found in footer``。写到同目录下的临时文件再
        ``os.replace`` 保证读取者要么看到旧文件、要么看到完整新文件。
        """
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            frame.to_parquet(tmp_path, index=False)
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def invalidate_gap_profile(self) -> None:
        """写入后丢弃缺口画像与股票池计数缓存，让下一次读取反映最新数据。"""
        with self._gap_profile_lock:
            self._gap_profile_cache = None
        with self._symbol_count_lock:
            self._symbol_count_cache = None

    def cached_symbol_count(self) -> int | None:
        """本地 OHLC 股票池规模（只读缓存；``None`` = 缓存未热）。

        供热路径（红绿家数 provider 循环等有 deadline 的链路）消费：缓存未热时
        返回 ``None`` 而不是现算，调用方应记一条 diagnostics 并继续走自己的链路。
        现算请走 :meth:`refresh_symbol_count`（应在后台线程调用）。
        """
        with self._symbol_count_lock:
            cached = self._symbol_count_cache
            if cached is not None and time.monotonic() - cached[0] < SYMBOL_COUNT_TTL_SECONDS:
                return cached[1]
        return None

    def refresh_symbol_count(self) -> int:
        """现算本地股票池规模并写入缓存（TTL 600s，写入失效）。

        取最新年分区的 ``symbol`` 列做去重计数——单列扫描远轻于 coverage() 的
        全仓多列扫描；最新分区读不出来时退回 coverage() 口径。真实数据仓
        coverage() 实测 ~10s，本方法只应在后台预热等非 deadline 路径调用。
        """
        paths = self._partition_paths_for_range(None, None)
        count: int | None = None
        if paths:
            try:
                table = pq.read_table(paths[-1], columns=["symbol"])
                if table.num_rows:
                    count = len(set(table.column("symbol").to_pylist()))
            except Exception:  # noqa: BLE001 - 退化路径走 coverage() 全口径
                count = None
        if count is None:
            for item in self.coverage():
                if item.dataset == "daily_bars":
                    count = int(item.symbols or 0)
                    break
        count = int(count or 0)
        with self._symbol_count_lock:
            self._symbol_count_cache = (time.monotonic(), count)
        return count

    def read_daily_bars(
        self,
        symbols: Sequence[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        require_ohlc: bool = False,
    ) -> pd.DataFrame:
        paths = self._partition_paths_for_range(start_date, end_date)
        if not paths:
            return pd.DataFrame()
        frames = []
        for path in paths:
            frame = self._safe_read_parquet(path)
            if not frame.empty:
                frames.append(frame)
        if not frames:
            return pd.DataFrame()
        frame = pd.concat(frames, ignore_index=True)
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        if symbols:
            selected = {str(symbol).strip() for symbol in symbols if str(symbol).strip()}
            frame = frame[frame["symbol"].astype(str).isin(selected)]
        if start_date:
            frame = frame[frame["trade_date"] >= pd.Timestamp(start_date)]
        if end_date:
            frame = frame[frame["trade_date"] <= pd.Timestamp(end_date)]
        if require_ohlc:
            frame = _require_ohlc_rows(frame)
        return frame.sort_values(["symbol", "trade_date"]).reset_index(drop=True)

    def read_daily_symbols(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        require_ohlc: bool = False,
    ) -> list[str]:
        paths = self._partition_paths_for_range(start_date, end_date)
        if not paths:
            return []
        symbols: set[str] = set()
        for path in paths:
            try:
                available_columns = set(pq.ParquetFile(path).schema_arrow.names)
            except FileNotFoundError:
                continue
            selected_columns = ["symbol"]
            if start_date or end_date:
                selected_columns.append("trade_date")
            if require_ohlc:
                selected_columns.extend(OHLC_COLUMNS)
            selected_columns = [column for column in selected_columns if column in available_columns]
            if "symbol" not in selected_columns:
                continue
            if (start_date or end_date) and "trade_date" not in selected_columns:
                continue
            if require_ohlc and not all(column in selected_columns for column in OHLC_COLUMNS):
                continue

            frame = self._safe_read_parquet(path, columns=selected_columns)
            if frame.empty:
                continue
            if start_date or end_date:
                frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
                frame = frame.dropna(subset=["trade_date"])
                if start_date:
                    frame = frame[frame["trade_date"] >= pd.Timestamp(start_date)]
                if end_date:
                    frame = frame[frame["trade_date"] <= pd.Timestamp(end_date)]
            if require_ohlc:
                frame = _require_ohlc_rows(frame)
            if frame.empty:
                continue
            symbols.update(str(symbol) for symbol in frame["symbol"].dropna().astype(str).unique())
        return sorted(symbol for symbol in symbols if symbol)

    def read_latest_daily_bars(self, days: int = 2) -> pd.DataFrame:
        paths = sorted(self.daily_bars_root.glob("year=*/daily_bars.parquet"), reverse=True)
        if not paths:
            return pd.DataFrame()

        frames: list[pd.DataFrame] = []
        unique_dates: set[pd.Timestamp] = set()
        for path in paths:
            frame = self._safe_read_parquet(path)
            if frame.empty:
                continue
            frame["trade_date"] = pd.to_datetime(frame["trade_date"])
            frame = _require_ohlc_rows(frame)
            if frame.empty:
                continue
            frames.append(frame)
            unique_dates.update(pd.Timestamp(value) for value in frame["trade_date"].drop_duplicates().tolist())
            if len(unique_dates) >= days:
                break

        if not frames:
            return pd.DataFrame()
        combined = pd.concat(frames, ignore_index=True)
        latest_dates = sorted(combined["trade_date"].drop_duplicates().tolist())[-days:]
        return (
            combined[combined["trade_date"].isin(latest_dates)]
            .sort_values(["symbol", "trade_date"])
            .reset_index(drop=True)
        )

    def coverage(self, *, thin_day_ratio: float = 0.5) -> list[DatasetCoverage]:
        paths = sorted(self.daily_bars_root.glob("year=*/daily_bars.parquet"))
        if not paths:
            return [
                DatasetCoverage(dataset="daily_bars", symbols=0, start_date=None, end_date=None),
                DatasetCoverage(dataset="market_cap", symbols=0, start_date=None, end_date=None),
                DatasetCoverage(dataset="capital_flow", symbols=0, start_date=None, end_date=None),
            ]

        required_columns = ["symbol", "trade_date", "open", "high", "low", "close"]
        optional_columns = ["float_market_cap", "main_net_inflow"]
        wanted_columns = required_columns + optional_columns
        daily_symbols: set[str] = set()
        market_cap_symbols: set[str] = set()
        capital_flow_symbols: set[str] = set()
        daily_start: date | None = None
        daily_end: date | None = None
        market_cap_start: date | None = None
        market_cap_end: date | None = None
        capital_flow_start: date | None = None
        capital_flow_end: date | None = None
        daily_missing_rows = 0
        daily_suspension_rows = 0
        market_cap_missing_rows = 0
        capital_flow_missing_rows = 0
        first_daily_date_by_symbol: dict[str, pd.Timestamp] = {}
        flow_start_by_symbol: dict[str, pd.Timestamp] = {}
        missing_capital_flow_frames: list[pd.DataFrame] = []
        # 每个交易日的全市场 OHLC 完整行数：横截面证据，用于把内部洞分成
        # “停牌类（市场正常日，不可补）”与“疑似写入失败（thin day，可补）”。
        rows_per_date: dict[pd.Timestamp, int] = {}
        # 逐股统计（按年分区增量合并）：缺失行数改为“交易日历期望 − 实际持有”
        # 的累计口径，让“多日未同步的尾部缺口”可见，而不是只数最新一天。
        ohlc_rows_by_symbol: dict[str, int] = {}
        ohlc_last_by_symbol: dict[str, pd.Timestamp] = {}
        cap_rows_by_symbol: dict[str, int] = {}
        cap_first_by_symbol: dict[str, pd.Timestamp] = {}
        cap_last_by_symbol: dict[str, pd.Timestamp] = {}
        flow_rows_by_symbol: dict[str, int] = {}
        flow_last_by_symbol: dict[str, pd.Timestamp] = {}

        def update_range(current_start: date | None, current_end: date | None, frame: pd.DataFrame) -> tuple[date | None, date | None]:
            if frame.empty:
                return current_start, current_end
            part_start = frame["trade_date"].min().date()
            part_end = frame["trade_date"].max().date()
            return (
                part_start if current_start is None else min(current_start, part_start),
                part_end if current_end is None else max(current_end, part_end),
            )

        def accumulate_symbol_stats(
            frame: pd.DataFrame,
            *,
            rows: dict[str, int],
            first: dict[str, pd.Timestamp] | None,
            last: dict[str, pd.Timestamp],
        ) -> None:
            if frame.empty:
                return
            grouped = frame.groupby(frame["symbol"].astype(str))["trade_date"]
            counts = grouped.count().to_dict()
            mins = grouped.min().to_dict() if first is not None else {}
            maxs = grouped.max().to_dict()
            for symbol, count in counts.items():
                rows[symbol] = rows.get(symbol, 0) + int(count)
                if first is not None:
                    symbol_min = pd.Timestamp(mins[symbol])
                    if symbol not in first or symbol_min < first[symbol]:
                        first[symbol] = symbol_min
                symbol_max = pd.Timestamp(maxs[symbol])
                if symbol not in last or symbol_max > last[symbol]:
                    last[symbol] = symbol_max

        for path in paths:
            try:
                available_columns = set(pq.ParquetFile(path).schema_arrow.names)
            except FileNotFoundError:
                continue
            selected_columns = [column for column in wanted_columns if column in available_columns]
            if "symbol" not in selected_columns or "trade_date" not in selected_columns:
                continue

            frame = self._safe_read_parquet(path, columns=selected_columns)
            if frame.empty:
                continue
            frame["trade_date"] = pd.to_datetime(frame["trade_date"])

            present_ohlc = [column for column in OHLC_COLUMNS if column in frame]
            if len(present_ohlc) < 4:
                ohlc_complete = frame.iloc[0:0]
            else:
                ohlc_complete_mask = frame[present_ohlc].notna().all(axis=1)
                ohlc_complete = frame.loc[ohlc_complete_mask]

            if not ohlc_complete.empty:
                daily_symbols.update(str(symbol) for symbol in ohlc_complete["symbol"].dropna().astype(str).unique())
                daily_start, daily_end = update_range(daily_start, daily_end, ohlc_complete)
                daily_starts = (
                    ohlc_complete.groupby(ohlc_complete["symbol"].astype(str))["trade_date"]
                    .min()
                    .to_dict()
                )
                for symbol, trade_date in daily_starts.items():
                    current = first_daily_date_by_symbol.get(symbol)
                    timestamp = pd.Timestamp(trade_date)
                    if current is None or timestamp < current:
                        first_daily_date_by_symbol[symbol] = timestamp
                accumulate_symbol_stats(
                    ohlc_complete,
                    rows=ohlc_rows_by_symbol,
                    first=None,
                    last=ohlc_last_by_symbol,
                )
                for trade_date, count in ohlc_complete.groupby("trade_date").size().items():
                    key = pd.Timestamp(trade_date)
                    rows_per_date[key] = rows_per_date.get(key, 0) + int(count)

            if "float_market_cap" in ohlc_complete:
                market_cap_mask = ohlc_complete["float_market_cap"].notna()
                market_cap_frame = ohlc_complete.loc[market_cap_mask]
                market_cap_symbols.update(str(symbol) for symbol in market_cap_frame["symbol"].dropna().astype(str).unique())
                market_cap_missing_rows += int((~market_cap_mask).sum())
                market_cap_start, market_cap_end = update_range(market_cap_start, market_cap_end, market_cap_frame)
                accumulate_symbol_stats(
                    market_cap_frame,
                    rows=cap_rows_by_symbol,
                    first=cap_first_by_symbol,
                    last=cap_last_by_symbol,
                )
            else:
                market_cap_missing_rows += int(len(ohlc_complete))

            if "main_net_inflow" in frame:
                capital_flow_mask = frame["main_net_inflow"].notna()
                capital_flow_frame = frame.loc[capital_flow_mask]
                capital_flow_symbols.update(str(symbol) for symbol in capital_flow_frame["symbol"].dropna().astype(str).unique())
                capital_flow_start, capital_flow_end = update_range(capital_flow_start, capital_flow_end, capital_flow_frame)
                flow_starts = (
                    capital_flow_frame.groupby(capital_flow_frame["symbol"].astype(str))["trade_date"]
                    .min()
                    .to_dict()
                    if not capital_flow_frame.empty
                    else {}
                )
                for symbol, trade_date in flow_starts.items():
                    current = flow_start_by_symbol.get(symbol)
                    timestamp = pd.Timestamp(trade_date)
                    if current is None or timestamp < current:
                        flow_start_by_symbol[symbol] = timestamp
                accumulate_symbol_stats(
                    capital_flow_frame,
                    rows=flow_rows_by_symbol,
                    first=None,
                    last=flow_last_by_symbol,
                )
                if not ohlc_complete.empty:
                    missing_flow = frame.loc[
                        ohlc_complete.index[frame.loc[ohlc_complete.index, "main_net_inflow"].isna()],
                        ["symbol", "trade_date"],
                    ]
                    if not missing_flow.empty:
                        missing_capital_flow_frames.append(
                            missing_flow.assign(symbol=missing_flow["symbol"].astype(str))
                            .reset_index(drop=True)
                        )
            elif not ohlc_complete.empty:
                missing_capital_flow_frames.append(
                    ohlc_complete[["symbol", "trade_date"]]
                    .assign(symbol=ohlc_complete["symbol"].astype(str))
                    .reset_index(drop=True)
                )

        if missing_capital_flow_frames:
            missing_flow = pd.concat(missing_capital_flow_frames, ignore_index=True)
            missing_flow["trade_date"] = pd.to_datetime(missing_flow["trade_date"], errors="coerce")
            missing_flow = missing_flow.dropna(subset=["trade_date"])
            if not missing_flow.empty:
                missing_flow = missing_flow.loc[
                    ~missing_flow["trade_date"].isin(KNOWN_CAPITAL_FLOW_SOURCE_GAP_DATES)
                ]
            if not missing_flow.empty and flow_start_by_symbol:
                source_start_symbols = {
                    symbol
                    for symbol, flow_start in flow_start_by_symbol.items()
                    if uses_symbol_capital_flow_source_start(
                        symbol,
                        flow_start,
                        first_daily_date_by_symbol.get(symbol),
                        pd.Timestamp(daily_start) if daily_start is not None else None,
                    )
                }
                if source_start_symbols:
                    mapped_flow_start = missing_flow["symbol"].map(flow_start_by_symbol)
                    before_source_start = (
                        missing_flow["symbol"].isin(source_start_symbols)
                        & mapped_flow_start.notna()
                        & (missing_flow["trade_date"] < mapped_flow_start)
                    )
                    missing_flow = missing_flow.loc[~before_source_start]
            capital_flow_missing_rows += int(len(missing_flow))

        lifecycle = self.read_symbol_lifecycle()
        if daily_symbols and daily_end is not None:
            # 日线缺口 = 内部洞（分类后）+ 停更尾部：
            # - 内部洞按横截面证据分类：市场正常日的缺行是停牌（公开渠道天然
            #   没有停牌日 K 线，不可补，单列 suspension_rows）；thin day（当日
            #   全市场行数异常低，疑似写入失败）的缺行才是可行动缺口。
            #   2025 年实测：15,095 个缺口对 100% 落在市场正常日——旧口径把
            #   它们全数计入缺失，数字因此基本虚假。
            # - 停更尾部（最后一条行 → 最新交易日）保持可行动口径，它是
            #   “这只股票多久没同步”的信号，绝不参与停牌分类。
            internal_missing, internal_suspension = self._classified_internal_missing(
                first_by_symbol=first_daily_date_by_symbol,
                last_by_symbol=ohlc_last_by_symbol,
                rows_per_date=rows_per_date,
                window_end=pd.Timestamp(daily_end),
                lifecycle=lifecycle,
                thin_day_ratio=thin_day_ratio,
            )
            daily_missing_rows += internal_missing
            daily_suspension_rows += internal_suspension
            daily_missing_rows += self._tail_missing_rows(
                symbols=set(ohlc_last_by_symbol),
                boundary_by_symbol=ohlc_last_by_symbol,
                window_end=pd.Timestamp(daily_end),
                lifecycle=lifecycle,
            )
            # 市值/资金流：内部缺口（已有行但字段为空）沿用原统计；这里只补
            # “最后一条数据之后到最新交易日的尾部停更缺口”——旧口径下股票
            # 一旦停止写入就显示 0 缺口，多日未同步完全不可见。
            market_cap_missing_rows += self._tail_missing_rows(
                symbols=set(cap_rows_by_symbol) | {s for s in daily_symbols if s not in cap_rows_by_symbol},
                boundary_by_symbol={
                    **{s: ohlc_last_by_symbol[s] for s in daily_symbols if s in ohlc_last_by_symbol},
                    **{s: cap_last_by_symbol[s] for s in cap_last_by_symbol if s not in ohlc_last_by_symbol},
                },
                window_end=pd.Timestamp(daily_end),
                lifecycle=lifecycle,
            )
            # 资金流尾部边界：取日线末行与资金流末行的较大者。§8 允许“暂无
            # 日 K 也先写资金流独立行”，独立行会把资金流末行推到日线末行之后；
            # 这段日期已有资金流数据，不能再按日线末行计成缺口——否则补齐
            # 资金流后 coverage 的缺口永远不降，与缺口画像“已最新”互相矛盾。
            capital_flow_boundary_by_symbol: dict[str, pd.Timestamp] = dict(ohlc_last_by_symbol)
            for flow_symbol, flow_last in flow_last_by_symbol.items():
                current = capital_flow_boundary_by_symbol.get(flow_symbol)
                if current is None or flow_last > current:
                    capital_flow_boundary_by_symbol[flow_symbol] = flow_last
            capital_flow_missing_rows += self._tail_missing_rows(
                symbols=set(capital_flow_boundary_by_symbol),
                boundary_by_symbol=capital_flow_boundary_by_symbol,
                window_end=pd.Timestamp(daily_end),
                lifecycle=lifecycle,
            )

        return [
            DatasetCoverage(
                dataset="daily_bars",
                symbols=len(daily_symbols),
                start_date=daily_start,
                end_date=daily_end,
                missing_rows=daily_missing_rows,
                suspension_rows=daily_suspension_rows,
            ),
            DatasetCoverage(
                dataset="market_cap",
                symbols=len(market_cap_symbols),
                start_date=market_cap_start,
                end_date=market_cap_end,
                missing_rows=market_cap_missing_rows,
            ),
            DatasetCoverage(
                dataset="capital_flow",
                symbols=len(capital_flow_symbols),
                start_date=capital_flow_start,
                end_date=capital_flow_end,
                missing_rows=capital_flow_missing_rows,
            ),
        ]

    def _trading_dates_between(self, start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
        if end <= start:
            return []
        return sorted(a_share_trade_dates(start, end))

    def _classified_internal_missing(
        self,
        *,
        first_by_symbol: dict[str, pd.Timestamp],
        last_by_symbol: dict[str, pd.Timestamp],
        rows_per_date: dict[pd.Timestamp, int],
        window_end: pd.Timestamp,
        lifecycle: dict[str, dict[str, str | None]],
        thin_day_ratio: float,
    ) -> tuple[int, int]:
        """内部洞（首行 ~ 末行之间的缺行）按横截面证据分类。

        对窗口内每个交易日 d：内部缺行数 = spanning(d) − 实有行数(d)，其中
        spanning(d) = 满足 [起, 止]（生命周期截断后）覆盖 d 的股票数。某股票
        在 d 有行则必然 spanning d，因此实有 ≤ spanning 恒成立。

        分类：实有行数低于 ``median × thin_day_ratio`` 的交易日是 thin day
        （疑似写入失败），其缺行计入 missing_rows（可行动）；其余交易日的
        缺行是停牌类（公开渠道天然没有，不可补），计入 suspension_rows。

        返回 (missing_rows, suspension_rows)。无生命周期记录的股票按在市
        处理（保守口径不变）。
        """
        starts: list[int] = []
        ends: list[int] = []
        for symbol, first in first_by_symbol.items():
            last = last_by_symbol.get(symbol)
            if last is None:
                continue
            record = lifecycle.get(symbol)
            listing = lifecycle_bound(record, "listing_date")
            delisted = lifecycle_bound(record, "delisted_date")
            span_start = pd.Timestamp(first)
            span_end = pd.Timestamp(last)
            if listing is not None and pd.Timestamp(listing) > span_start:
                span_start = pd.Timestamp(listing)
            if delisted is not None and pd.Timestamp(delisted) < span_end:
                span_end = pd.Timestamp(delisted)
            if span_start > span_end:
                continue
            starts.append(span_start.value)
            ends.append(span_end.value)
        if not starts:
            return 0, 0
        bounds_start = pd.Timestamp(min(starts))
        if window_end <= bounds_start:
            return 0, 0
        calendar = self._trading_dates_between(bounds_start, window_end)
        if not calendar:
            return 0, 0
        starts.sort()
        ends.sort()
        threshold = max(1.0, float(median(rows_per_date.values())) * thin_day_ratio)
        missing_total = 0
        suspension_total = 0
        for day in calendar:
            day_value = day.value
            spanning = bisect.bisect_right(starts, day_value) - bisect.bisect_left(ends, day_value)
            internal = spanning - rows_per_date.get(day, 0)
            if internal <= 0:
                continue
            if rows_per_date.get(day, 0) < threshold:
                missing_total += internal
            else:
                suspension_total += internal
        return missing_total, suspension_total

    def _tail_missing_rows(
        self,
        *,
        symbols: set[str],
        boundary_by_symbol: dict[str, pd.Timestamp],
        window_end: pd.Timestamp,
        lifecycle: dict[str, dict[str, str | None]],
    ) -> int:
        """统计每只股票“最后一条数据之后”到最新交易日之间的交易日数。

        边界由调用方给出（市值取 OHLC 末行；资金流取 OHLC 末行与资金流末行
        的较大者，独立资金流行覆盖的日期不算缺失），边界之后的交易日整段
        无行，不会与“已有行但字段为空”的内部缺口重复计数。
        """
        if not symbols or not boundary_by_symbol:
            return 0
        bounds = [value for value in boundary_by_symbol.values() if value is not None]
        if not bounds or window_end <= min(bounds):
            return 0
        calendar = self._trading_dates_between(min(bounds), window_end)
        if not calendar:
            return 0
        total = 0
        for symbol in symbols:
            boundary = boundary_by_symbol.get(symbol)
            if boundary is None:
                continue
            record = lifecycle.get(symbol)
            delisted = lifecycle_bound(record, "delisted_date")
            sym_end = min(window_end, pd.Timestamp(delisted)) if delisted is not None else window_end
            if sym_end <= boundary:
                continue
            lo = bisect.bisect_right(calendar, boundary)
            hi = bisect.bisect_right(calendar, sym_end)
            total += max(0, hi - lo)
        return total

    def data_gap_profile(
        self,
        *,
        partition_years: int = GAP_PROFILE_DEFAULT_PARTITION_YEARS,
        thin_day_ratio: float = 0.5,
        top_stale: int = GAP_PROFILE_TOP_STALE,
    ) -> dict[str, object]:
        """缺口画像：不只“缺多少行”，而是“具体缺在哪”。

        - 停更分布：多少只股票的数据停在哪个日期（多日未同步的尾部）；
        - 薄行日：行数远低于中位数的交易日（旧写入失败的可疑日期）；
        - 市值/资金流的字段停更尾部。

        已标记退市（``symbol_lifecycle``）的股票不计入停更分布，单列
        ``delisted_symbols``——退市股的停更是终态而非缺口，混在一起会让
        UI 与 AI 把“退市”误读成“需要补数据”。

        只读最近 ``partition_years`` 个年分区（最新数据必然在其中），带
        10 分钟缓存——AI 工具与诊断端点共用，避免每次全仓扫描；画像窗口
        不覆盖的更早分区里的停更股票不会出现在分布里。
        """
        with self._gap_profile_lock:
            cached = self._gap_profile_cache
            if cached is not None and time.monotonic() - cached[0] < GAP_PROFILE_TTL_SECONDS:
                return cached[1]
        profile = self._compute_gap_profile(
            partition_years=partition_years,
            thin_day_ratio=thin_day_ratio,
            top_stale=top_stale,
        )
        with self._gap_profile_lock:
            self._gap_profile_cache = (time.monotonic(), profile)
        return profile

    def _compute_gap_profile(
        self,
        *,
        partition_years: int,
        thin_day_ratio: float,
        top_stale: int,
    ) -> dict[str, object]:
        paths = sorted(self.daily_bars_root.glob("year=*/daily_bars.parquet"))
        if not paths:
            return {"available": False, "reason": "本地数据仓还没有日线分区。"}
        selected = paths[-max(1, partition_years) :]
        wanted_columns = ["symbol", "trade_date", *OHLC_COLUMNS, "float_market_cap", "main_net_inflow"]
        last_ohlc: dict[str, pd.Timestamp] = {}
        last_cap: dict[str, pd.Timestamp] = {}
        last_flow: dict[str, pd.Timestamp] = {}
        rows_per_date: dict[pd.Timestamp, int] = {}
        for path in selected:
            try:
                available = set(pq.ParquetFile(path).schema_arrow.names)
            except FileNotFoundError:
                continue
            columns = [column for column in wanted_columns if column in available]
            if "symbol" not in columns or "trade_date" not in columns:
                continue
            frame = self._safe_read_parquet(path, columns=columns)
            if frame.empty:
                continue
            frame["trade_date"] = pd.to_datetime(frame["trade_date"])
            present_ohlc = [column for column in OHLC_COLUMNS if column in frame]
            complete = (
                frame.loc[frame[present_ohlc].notna().all(axis=1)] if len(present_ohlc) >= 4 else frame.iloc[0:0]
            )
            if not complete.empty:
                for symbol, last_date in complete.groupby(complete["symbol"].astype(str))["trade_date"].max().items():
                    timestamp = pd.Timestamp(last_date)
                    if symbol not in last_ohlc or timestamp > last_ohlc[symbol]:
                        last_ohlc[symbol] = timestamp
                for trade_date, count in complete.groupby("trade_date").size().items():
                    key = pd.Timestamp(trade_date)
                    rows_per_date[key] = rows_per_date.get(key, 0) + int(count)
            if "float_market_cap" in complete:
                cap_frame = complete.loc[complete["float_market_cap"].notna()]
                for symbol, last_date in cap_frame.groupby(cap_frame["symbol"].astype(str))["trade_date"].max().items():
                    timestamp = pd.Timestamp(last_date)
                    if symbol not in last_cap or timestamp > last_cap[symbol]:
                        last_cap[symbol] = timestamp
            if "main_net_inflow" in frame:
                # 资金流允许独立行（无 OHLC），从全 frame 统计。
                flow_frame = frame.loc[frame["main_net_inflow"].notna()]
                for symbol, last_date in flow_frame.groupby(flow_frame["symbol"].astype(str))["trade_date"].max().items():
                    timestamp = pd.Timestamp(last_date)
                    if symbol not in last_flow or timestamp > last_flow[symbol]:
                        last_flow[symbol] = timestamp
        if not rows_per_date or not last_ohlc:
            return {"available": False, "reason": "选中分区内没有可用的日线行。"}
        try:
            lifecycle = self.read_symbol_lifecycle()
        except Exception:  # noqa: BLE001 - 画像读不到生命周期时退回保守口径
            lifecycle = {}
        delisted_symbols = {
            symbol
            for symbol, record in lifecycle.items()
            if record.get("status") == "delisted" or record.get("delisted_date")
        }
        daily_end = max(rows_per_date)
        window_start = min(rows_per_date)

        def split_delisted(last_by_symbol: dict[str, pd.Timestamp]) -> tuple[dict[str, pd.Timestamp], int]:
            active = {symbol: ts for symbol, ts in last_by_symbol.items() if symbol not in delisted_symbols}
            return active, len(last_by_symbol) - len(active)

        active_ohlc_last, ohlc_delisted = split_delisted(last_ohlc)
        active_cap_last, cap_delisted = split_delisted(last_cap)
        active_flow_last, flow_delisted = split_delisted(last_flow)
        current_symbols = sum(1 for timestamp in active_ohlc_last.values() if timestamp >= daily_end)
        thin_days: list[dict[str, object]] = []
        if len(rows_per_date) >= 4:
            median_rows = median(rows_per_date.values())
            threshold = max(1.0, median_rows * thin_day_ratio)
            thin_days = [
                {"trade_date": trade_date.date().isoformat(), "rows": count}
                for trade_date, count in sorted(rows_per_date.items())
                if count < threshold
            ][:20]

        def stale_entries(last_by_symbol: dict[str, pd.Timestamp]) -> list[dict[str, object]]:
            counter = Counter(
                timestamp.date().isoformat() for timestamp in last_by_symbol.values() if timestamp < daily_end
            )
            return [
                {"last_date": last_date, "symbols": count}
                for last_date, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:top_stale]
            ]

        profile: dict[str, object] = {
            "available": True,
            "window": {
                "start_date": window_start.date().isoformat(),
                "end_date": daily_end.date().isoformat(),
                "partitions": [path.parent.name for path in selected],
            },
            "daily_bars": {
                "symbols": len(last_ohlc),
                "symbols_current": current_symbols,
                "symbols_stale": len(active_ohlc_last) - current_symbols,
                "delisted_symbols": ohlc_delisted,
                "stale_distribution": stale_entries(active_ohlc_last),
                "thin_days": thin_days,
            },
            "market_cap": {
                "symbols": len(last_cap),
                "delisted_symbols": cap_delisted,
                "stale_distribution": stale_entries(active_cap_last),
            },
            "capital_flow": {
                "symbols": len(last_flow),
                "delisted_symbols": flow_delisted,
                "stale_distribution": stale_entries(active_flow_last),
            },
        }
        return profile

    def upsert_symbol_lifecycle(self, rows: Sequence[dict[str, str | None]]) -> int:
        """Insert or update ``symbol_lifecycle`` rows.

        Each row needs ``symbol`` and may carry ``listing_date`` /
        ``delisted_date`` (ISO strings or ``None``) plus ``status``
        (``listed`` / ``delisted`` / ``unknown``). Returns the number of rows
        written; the write is atomic per call.
        """
        payload = []
        for row in rows:
            symbol = str(row.get("symbol", "")).strip()
            if not symbol:
                continue
            status = str(row.get("status") or "").strip()
            if not status:
                status = "delisted" if row.get("delisted_date") else "listed"
            payload.append(
                (
                    symbol,
                    _clean_lifecycle_date(row.get("listing_date")),
                    _clean_lifecycle_date(row.get("delisted_date")),
                    status,
                )
            )
        if not payload:
            return 0
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.executemany(
                """
                INSERT INTO symbol_lifecycle (symbol, listing_date, delisted_date, status, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(symbol) DO UPDATE SET
                    listing_date = COALESCE(excluded.listing_date, symbol_lifecycle.listing_date),
                    delisted_date = COALESCE(excluded.delisted_date, symbol_lifecycle.delisted_date),
                    status = excluded.status,
                    updated_at = CURRENT_TIMESTAMP
                """,
                payload,
            )
        # 生命周期变更会改变缺口画像的退市剔除口径，让下次读取重新计算。
        with self._gap_profile_lock:
            self._gap_profile_cache = None
        return len(payload)

    def read_symbol_lifecycle(self, symbols: Sequence[str] | None = None) -> dict[str, dict[str, str | None]]:
        """Return ``{symbol: {listing_date, delisted_date, status}}`` records.

        ``listing_date`` / ``delisted_date`` are ISO strings or ``None``;
        symbols without a lifecycle record are absent from the mapping, and the
        caller is expected to fall back to the conservative (unclipped) window.
        """
        selected = None
        if symbols is not None:
            selected = {str(symbol).strip() for symbol in symbols if str(symbol).strip()}
            if not selected:
                return {}
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            if selected is None:
                rows = conn.execute("SELECT symbol, listing_date, delisted_date, status FROM symbol_lifecycle").fetchall()
            else:
                marks = ",".join("?" for _ in selected)
                rows = conn.execute(
                    f"SELECT symbol, listing_date, delisted_date, status FROM symbol_lifecycle WHERE symbol IN ({marks})",
                    tuple(sorted(selected)),
                ).fetchall()
        return {
            str(row["symbol"]): {
                "listing_date": row["listing_date"],
                "delisted_date": row["delisted_date"],
                "status": row["status"],
            }
            for row in rows
        }

    def read_delisted_symbols(self) -> set[str]:
        """Symbols whose lifecycle record marks them delisted (delisted_date set)."""
        with sqlite3.connect(self.sqlite_path) as conn:
            rows = conn.execute(
                "SELECT symbol FROM symbol_lifecycle WHERE delisted_date IS NOT NULL OR status = 'delisted'"
            ).fetchall()
        return {str(row[0]) for row in rows}

    def read_capital_flow_missing_symbols(self, start_date: str, end_date: str) -> set[str]:
        """Return symbols with no ``main_net_inflow`` value within
        ``[start_date, end_date]`` across the daily-bars partitions the window
        touches.

        只读最新分区会让横跨旧分区的补数窗口漏掉旧分区里的缺流股票
        （例如 12 月~1 月的窗口漏掉 ``year=2025`` 分区），因此按窗口覆盖
        的年份选择分区；一个分区都选不中时退回最新分区保持旧行为。

        Rows outside a symbol's ``symbol_lifecycle`` window (before listing /
        after delisting) are ignored, so a delisted stock no longer reports its
        post-delisting dates as missing. Symbols without a lifecycle record
        keep the conservative legacy behaviour.

        Encapsulates the parquet layout so HTTP/service layers do not need to
        know how daily bars are stored.
        """
        paths = sorted(self.daily_bars_root.glob("year=*/daily_bars.parquet"))
        if not paths:
            return set()
        window_start = pd.Timestamp(start_date)
        window_end = pd.Timestamp(end_date)
        selected: list[Path] = []
        for path in paths:
            try:
                year = int(path.parent.name.split("=", maxsplit=1)[1])
            except (IndexError, ValueError):
                continue
            if window_start.year <= year <= window_end.year:
                selected.append(path)
        if not selected:
            selected = [paths[-1]]
        columns_to_read = ["symbol", "trade_date", "main_net_inflow"]
        frames: list[pd.DataFrame] = []
        for path in selected:
            try:
                available = set(pq.ParquetFile(path).schema_arrow.names)
            except FileNotFoundError:
                continue
            columns = [column for column in columns_to_read if column in available]
            if "symbol" not in columns or "main_net_inflow" not in columns:
                continue
            frame = self._safe_read_parquet(path, columns=columns)
            if not frame.empty:
                frames.append(frame)
        if not frames:
            return set()
        frame = pd.concat(frames, ignore_index=True)
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        frame = frame.dropna(subset=["trade_date"])
        frame = frame[frame["trade_date"] >= window_start]
        frame = frame[frame["trade_date"] <= window_end]
        if frame.empty:
            return set()
        lifecycle = self.read_symbol_lifecycle(
            [str(symbol) for symbol in frame["symbol"].dropna().astype(str).unique()]
        )
        if lifecycle:
            listing_by_symbol = frame["symbol"].astype(str).map(
                lambda symbol: _lifecycle_bound(lifecycle.get(symbol), "listing_date")
            )
            delisted_by_symbol = frame["symbol"].astype(str).map(
                lambda symbol: _lifecycle_bound(lifecycle.get(symbol), "delisted_date")
            )
            in_window = (
                (listing_by_symbol.isna() | (frame["trade_date"] >= listing_by_symbol))
                & (delisted_by_symbol.isna() | (frame["trade_date"] <= delisted_by_symbol))
            )
            frame = frame.loc[in_window]
        if frame.empty:
            return set()
        has_any_flow = frame.groupby(frame["symbol"].astype(str))["main_net_inflow"].any()
        return set(has_any_flow[~has_any_flow].index)

    def _safe_read_parquet(self, path: Path, **kwargs) -> pd.DataFrame:
        """Read a partition, distinguishing "absent" from "unreadable".

        A missing file legitimately means "no data yet" and yields an empty
        frame.  A file that exists but cannot be parsed means the partition is
        corrupt — silently returning an empty frame there disguises corruption
        as *missing rows*, which sends sync into a re-download loop that gets
        overwritten again next round.  The original error is re-raised
        unchanged, and the path is recorded in ``corrupt_partitions`` so the UI
        and AI tools can tell "corrupt" apart from "not collected yet".
        """
        try:
            return pd.read_parquet(path, **kwargs)
        except FileNotFoundError:  # 文件不存在 == 还没有数据
            return pd.DataFrame()
        except Exception as exc:  # noqa: BLE001 - re-raised unchanged
            if path.exists():
                self._note_corrupt_partition(path, exc)
            raise

    def _note_corrupt_partition(self, path: Path, exc: Exception) -> None:
        with self._corrupt_partitions_lock:
            self._corrupt_partitions[str(path)] = str(exc)

    @property
    def corrupt_partitions(self) -> dict[str, str]:
        """``{partition_path: error}`` for partitions that failed to parse.

        Diagnostic surface for the UI/AI tools: a non-empty result means the
        warehouse has a real corruption problem, not a coverage gap.
        """
        with self._corrupt_partitions_lock:
            return dict(self._corrupt_partitions)

    def clear_corrupt_partitions(self) -> None:
        with self._corrupt_partitions_lock:
            self._corrupt_partitions.clear()


def _require_ohlc_rows(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    if not all(column in frame for column in OHLC_COLUMNS):
        return pd.DataFrame()
    return frame.dropna(subset=OHLC_COLUMNS)


def _clean_lifecycle_date(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "nat"):
        return None
    return text[:10]


def lifecycle_bound(record: dict[str, str | None] | None, key: str) -> pd.Timestamp | None:
    """Parse a lifecycle date field (``listing_date``/``delisted_date``) into a
    ``pd.Timestamp``; returns ``None`` for missing records or values."""
    if not record:
        return None
    raw = record.get(key)
    if not raw:
        return None
    try:
        return pd.Timestamp(str(raw)[:10])
    except ValueError:
        return None


_lifecycle_bound = lifecycle_bound


def uses_symbol_capital_flow_source_start(
    symbol: str,
    flow_start: pd.Timestamp,
    first_daily_date: pd.Timestamp | None = None,
    warehouse_start: pd.Timestamp | None = None,
) -> bool:
    if (
        symbol in KNOWN_CAPITAL_FLOW_SOURCE_START_SYMBOLS
        or symbol.startswith(KNOWN_CAPITAL_FLOW_SOURCE_START_SYMBOL_PREFIXES)
        or pd.Timestamp(flow_start) in KNOWN_CAPITAL_FLOW_SOURCE_START_DATES
    ):
        return True
    if first_daily_date is None or warehouse_start is None:
        return False
    first_daily = pd.Timestamp(first_daily_date)
    start = pd.Timestamp(warehouse_start)
    if first_daily <= start:
        return False
    lag_days = (pd.Timestamp(flow_start) - first_daily).days
    return 0 <= lag_days <= KNOWN_CAPITAL_FLOW_LISTING_LAG_DAYS
