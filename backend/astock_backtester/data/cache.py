from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

import pandas as pd

from astock_backtester.data.filelock import CrossProcessFileLock
from astock_backtester.data.importer import normalize_daily_bars
from astock_backtester.models import DatasetCoverage

logger = logging.getLogger(__name__)


class LocalCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.parquet_dir = self.root / "parquet"
        self.parquet_dir.mkdir(parents=True, exist_ok=True)
        self.sqlite_path = self.root / "metadata.sqlite"
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS datasets (
                    dataset TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    @property
    def daily_bars_path(self) -> Path:
        return self.parquet_dir / "daily_bars.parquet"

    @property
    def daily_bars_pickle_path(self) -> Path:
        return self.parquet_dir / "daily_bars.pkl"

    def write_daily_bars(self, frame: pd.DataFrame) -> None:
        normalized = normalize_daily_bars(frame)
        self.parquet_dir.mkdir(parents=True, exist_ok=True)
        # 跨进程互斥：read-modify-write 必须全程持锁，否则并发写者交错会丢失更新。
        # 读侧（read_daily_bars/coverage）不加锁：os.replace 原子替换保证读取者
        # 只会看到旧文件或完整新文件，且本方法内部会调 read_daily_bars，读侧加锁会自锁。
        with CrossProcessFileLock(self.daily_bars_path):
            current = self.read_daily_bars()
            if not current.empty:
                normalized = (
                    normalized.set_index(["symbol", "trade_date"])
                    .combine_first(current.set_index(["symbol", "trade_date"]))
                    .reset_index()
                    .sort_values(["symbol", "trade_date"])
                    .reset_index(drop=True)
                )
            try:
                self._atomic_write_parquet(normalized, self.daily_bars_path)
                if self.daily_bars_pickle_path.exists():
                    self.daily_bars_pickle_path.unlink()
            except ImportError:
                self._atomic_write_pickle(normalized, self.daily_bars_pickle_path)
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO datasets(dataset, updated_at) VALUES('daily_bars', CURRENT_TIMESTAMP)"
                )

    @staticmethod
    def _atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
        """临时文件写完后 ``os.replace`` 原子替换。

        ``to_parquet`` 直接写目标路径时会先 truncate 再逐块写，写一半崩溃会
        留下损坏 parquet。写到同目录下的临时文件再 ``os.replace`` 保证读取者
        要么看到旧文件、要么看到完整新文件。
        """
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            frame.to_parquet(tmp_path, index=False)
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _atomic_write_pickle(frame: pd.DataFrame, path: Path) -> None:
        """pickle 兜底路径同样走临时文件 + ``os.replace``，不直写目标。"""
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            frame.to_pickle(tmp_path)
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def read_daily_bars(self) -> pd.DataFrame:
        if self.daily_bars_path.exists():
            try:
                return pd.read_parquet(self.daily_bars_path).sort_values(["symbol", "trade_date"]).reset_index(drop=True)
            except Exception as exc:
                logger.warning(
                    "cached daily-bars parquet read failed for %s: %s",
                    self.daily_bars_path,
                    exc,
                    exc_info=True,
                )
        if self.daily_bars_pickle_path.exists():
            try:
                return pd.read_pickle(self.daily_bars_pickle_path).sort_values(["symbol", "trade_date"]).reset_index(drop=True)
            except Exception as exc:
                logger.warning(
                    "cached daily-bars pickle read failed for %s: %s",
                    self.daily_bars_pickle_path,
                    exc,
                    exc_info=True,
                )
        return pd.DataFrame()

    def coverage(self) -> list[DatasetCoverage]:
        bars = self.read_daily_bars()
        if bars.empty:
            return [
                DatasetCoverage(dataset="daily_bars", symbols=0, start_date=None, end_date=None),
                DatasetCoverage(dataset="capital_flow", symbols=0, start_date=None, end_date=None),
                DatasetCoverage(dataset="market_cap", symbols=0, start_date=None, end_date=None),
            ]
        start_date = bars["trade_date"].min().date()
        end_date = bars["trade_date"].max().date()
        return [
            DatasetCoverage(
                dataset="daily_bars",
                symbols=int(bars["symbol"].nunique()),
                start_date=start_date,
                end_date=end_date,
                missing_rows=int(bars[["open", "high", "low", "close"]].isna().any(axis=1).sum()),
            ),
            DatasetCoverage(
                dataset="capital_flow",
                symbols=int(bars.loc[bars["main_net_inflow"].notna(), "symbol"].nunique()),
                start_date=start_date,
                end_date=end_date,
                missing_rows=int(bars["main_net_inflow"].isna().sum()),
            ),
            DatasetCoverage(
                dataset="market_cap",
                symbols=int(bars.loc[bars["float_market_cap"].notna(), "symbol"].nunique()),
                start_date=start_date,
                end_date=end_date,
                missing_rows=int(bars["float_market_cap"].isna().sum()),
            ),
        ]
