from __future__ import annotations

import time
from threading import Event
from time import sleep

import pandas as pd
from astock_backtester.data.cache import LocalCache
from astock_backtester.data.sync import SyncJobManager
from astock_backtester.data.warehouse import Warehouse


class FakeProvider:
    def __init__(self, fail_symbols=None):
        self.fail_symbols = set(fail_symbols or [])

    def fetch_daily_bars(self, symbol, start_date, end_date):
        if symbol in self.fail_symbols:
            raise RuntimeError("source unavailable")
        return pd.DataFrame(
            {
                "symbol": [symbol],
                "trade_date": [start_date],
                "open": [1.0],
                "high": [1.0],
                "low": [1.0],
                "close": [1.0],
                "volume": [1],
                "float_market_cap": [100.0],
                "total_market_cap": [120.0],
            }
        )


def test_daily_completeness_snapshot_is_lightweight_and_counter_is_vectorized(tmp_path):
    """快照只保留 pair 索引与布尔向量（内存爆炸修复），计数语义与逐行版本一致。"""
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                # 000001：OHLC 完整但市值缺失；000002：完整行；000003：仓里没有。
                "symbol": ["000001", "000002"],
                "trade_date": ["2026-06-17", "2026-06-17"],
                "open": [1.0, 1.0],
                "high": [1.0, 1.0],
                "low": [1.0, 1.0],
                "close": [1.0, 1.0],
                "volume": [1, 1],
                "float_market_cap": [float("nan"), 100.0],
                "total_market_cap": [float("nan"), 120.0],
            }
        )
    )
    manager = SyncJobManager(warehouse=warehouse, provider=FakeProvider())
    snapshot = manager._daily_completeness_snapshot("2026-06-17", "2026-06-18")

    assert isinstance(snapshot.existing_pairs, pd.MultiIndex)
    assert len(snapshot.existing_pairs) == 2
    assert snapshot.existing_ohlc_complete.all()
    assert snapshot.existing_cap_null.tolist() == [True, False]

    frame = pd.DataFrame(
        {
            "symbol": ["000001", "000002", "000003"],
            "trade_date": ["2026-06-17", "2026-06-18", "2026-06-18"],
            "open": [1.0, 1.0, 1.0],
            "high": [1.0, 1.0, 1.0],
            "low": [1.0, 1.0, 1.0],
            "close": [1.0, 1.0, 1.0],
            "volume": [1, 1, 1],
            "float_market_cap": [100.0, 100.0, 100.0],
            "total_market_cap": [120.0, 120.0, 120.0],
        }
    )
    filled = manager._count_full_market_filled_missing_rows(frame, "2026-06-17", "2026-06-18", snapshot)
    # 000001 06-17：已有 OHLC、原市值缺、新行带市值 → 市值补缺；
    # 000002 06-18 与 000003 06-18：快照中不存在该对且新行 OHLC 完整 → 日线补缺。
    assert filled.daily_rows == 2
    assert filled.market_cap_rows == 1
    assert filled.total == 3


def test_fill_counter_without_snapshot_counts_new_ohlc_rows(tmp_path):
    manager = SyncJobManager(warehouse=Warehouse(tmp_path), provider=FakeProvider())
    frame = pd.DataFrame(
        {
            "symbol": ["000001"],
            "trade_date": ["2026-06-17"],
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [1],
            "float_market_cap": [100.0],
            "total_market_cap": [120.0],
        }
    )
    filled = manager._count_full_market_filled_missing_rows(frame, None, None, None)
    assert filled.daily_rows == 1
    assert filled.market_cap_rows == 0


def test_full_market_job_counts_empty_provider_rows_as_failure(tmp_path):
    class EmptyProvider(FakeProvider):
        def fetch_daily_bars(self, symbol, start_date, end_date):
            if symbol == "000002":
                return pd.DataFrame()
            return super().fetch_daily_bars(symbol, start_date, end_date)

    warehouse = Warehouse(tmp_path)
    manager = SyncJobManager(warehouse=warehouse, provider=EmptyProvider())

    status = manager.run_full_market(
        symbols=["000001", "000002"],
        start_date="2015-01-01",
        end_date="2015-01-05",
    )

    assert status.completed_symbols == 1
    assert status.failed_symbols == 1
    assert status.status == "completed_with_errors"
    assert status.errors == ["000002: provider returned no daily rows"]
    assert warehouse.read_daily_bars()["symbol"].tolist() == ["000001"]


def test_full_market_job_persists_success_and_failure(tmp_path):
    warehouse = Warehouse(tmp_path)
    manager = SyncJobManager(warehouse=warehouse, provider=FakeProvider(fail_symbols={"000002"}))

    status = manager.run_full_market(
        symbols=["000001", "000002", "000003"],
        start_date="2015-01-01",
        end_date="2015-01-05",
    )

    assert status.total_symbols == 3
    assert status.completed_symbols == 2
    assert status.failed_symbols == 1
    assert status.imported_rows == 2
    loaded = warehouse.read_daily_bars()
    assert sorted(loaded["symbol"].tolist()) == ["000001", "000003"]


def test_full_market_job_can_run_asynchronously_and_report_progress(tmp_path):
    class SlowProvider(FakeProvider):
        def fetch_daily_bars(self, symbol, start_date, end_date):
            sleep(0.02)
            return super().fetch_daily_bars(symbol, start_date, end_date)

    warehouse = Warehouse(tmp_path)
    manager = SyncJobManager(warehouse=warehouse, provider=SlowProvider())

    status = manager.start_full_market(
        symbols=["000001", "000002", "000003"],
        start_date="2015-01-01",
        end_date="2015-01-05",
    )

    assert status.status == "running"
    assert status.total_symbols == 3
    eventually = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        eventually = manager.get_job(status.job_id)
        if eventually and eventually.status == "completed":
            break
        sleep(0.02)

    assert eventually is not None
    assert eventually.status == "completed"
    assert eventually.completed_symbols == 3
    assert warehouse.read_daily_bars()["symbol"].nunique() == 3


def test_full_market_job_flushes_each_symbol_batch_for_visible_progress(tmp_path):
    class CountingWarehouse(Warehouse):
        def __init__(self, cache_root):
            super().__init__(cache_root)
            self.write_batches = []

        def write_daily_bars(self, frame):
            self.write_batches.append(int(len(frame)))
            super().write_daily_bars(frame)

    warehouse = CountingWarehouse(tmp_path)
    manager = SyncJobManager(warehouse=warehouse, provider=FakeProvider())
    manager.full_market_batch_size = 25
    manager.full_market_write_batch_rows = 25_000
    symbols = [f"{index:06d}" for index in range(51)]

    status = manager.start_full_market(
        symbols=symbols,
        start_date="2026-06-09",
        end_date="2026-06-09",
    )

    eventually = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        eventually = manager.get_job(status.job_id)
        if eventually and eventually.status == "completed":
            break
        sleep(0.02)

    assert eventually is not None
    assert eventually.status == "completed"
    assert eventually.imported_rows == 51
    assert warehouse.write_batches == [25, 25, 1]


def test_async_full_market_job_flushes_successful_rows_after_a_batch_failure(tmp_path):
    class BlockingProvider(FakeProvider):
        def __init__(self):
            super().__init__(fail_symbols={"000002"})
            self.third_started = Event()
            self.release_third = Event()

        def fetch_daily_bars(self, symbol, start_date, end_date):
            if symbol == "000003":
                self.third_started.set()
                self.release_third.wait(timeout=5)
            return super().fetch_daily_bars(symbol, start_date, end_date)

    provider = BlockingProvider()
    warehouse = Warehouse(tmp_path)
    manager = SyncJobManager(
        warehouse=warehouse,
        provider=provider,
        full_market_batch_size=2,
        full_market_workers=1,
        full_market_write_batch_rows=25_000,
    )

    status = manager.start_full_market(
        symbols=["000001", "000002", "000003"],
        start_date="2026-06-09",
        end_date="2026-06-09",
    )

    assert provider.third_started.wait(timeout=5)
    running = manager.get_job(status.job_id)
    assert running is not None
    assert running.status == "running"
    assert running.completed_symbols == 1
    assert running.failed_symbols == 1
    assert sorted(warehouse.read_daily_bars()["symbol"].tolist()) == ["000001"]

    provider.release_third.set()
    eventually = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        eventually = manager.get_job(status.job_id)
        if eventually and eventually.status == "completed":
            break
        sleep(0.02)

    assert eventually is not None
    assert eventually.status == "completed_with_errors"
    assert sorted(warehouse.read_daily_bars()["symbol"].tolist()) == ["000001", "000003"]


def test_full_market_job_records_recent_failures_with_symbol_and_reason(tmp_path):
    warehouse = Warehouse(tmp_path)
    manager = SyncJobManager(warehouse=warehouse, provider=FakeProvider(fail_symbols={"000050"}))

    status = manager.start_full_market(
        symbols=["000050"],
        start_date="2026-06-18",
        end_date="2026-06-18",
    )

    eventually = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        eventually = manager.get_job(status.job_id)
        if eventually and eventually.status != "running":
            break
        sleep(0.02)

    assert eventually is not None
    assert eventually.status == "completed_with_errors"
    assert eventually.recent_failures == [{"symbol": "000050", "reason": "source unavailable"}]
    assert eventually.last_error == "000050: source unavailable"


def test_full_market_job_skips_symbols_already_complete_in_local_warehouse(tmp_path):
    class CountingProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.calls = []

        def fetch_daily_bars(self, symbol, start_date, end_date):
            self.calls.append(symbol)
            return super().fetch_daily_bars(symbol, start_date, end_date)

    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001"],
                "trade_date": ["2026-06-18"],
                "open": [10.0],
                "high": [10.5],
                "low": [9.8],
                "close": [10.2],
                "volume": [1000],
                "float_market_cap": [100.0],
                "total_market_cap": [120.0],
            }
        )
    )
    provider = CountingProvider()
    manager = SyncJobManager(warehouse=warehouse, provider=provider)

    status = manager.run_full_market(
        symbols=["000001", "000002"],
        start_date="2026-06-18",
        end_date="2026-06-19",
    )

    assert provider.calls == ["000002"]
    assert status.processed_symbols == 2
    assert status.skipped_symbols == 1
    assert status.completed_symbols == 1
    assert status.failed_symbols == 0
    assert status.status == "completed"


def test_full_market_job_fetches_symbols_with_missing_market_cap(tmp_path):
    class CountingProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.calls = []

        def fetch_daily_bars(self, symbol, start_date, end_date):
            self.calls.append(symbol)
            return super().fetch_daily_bars(symbol, start_date, end_date)

    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001"],
                "trade_date": ["2026-06-18"],
                "open": [10.0],
                "high": [10.5],
                "low": [9.8],
                "close": [10.2],
                "volume": [1000],
                "float_market_cap": [float("nan")],
                "total_market_cap": [float("nan")],
            }
        )
    )
    provider = CountingProvider()
    manager = SyncJobManager(warehouse=warehouse, provider=provider)

    status = manager.run_full_market(
        symbols=["000001"],
        start_date="2026-06-18",
        end_date="2026-06-18",
    )

    assert provider.calls == ["000001"]
    assert status.skipped_symbols == 0
    assert status.completed_symbols == 1
    assert status.imported_rows == 1
    assert status.filled_missing_rows == 1
    assert status.filled_daily_rows == 0
    assert status.filled_market_cap_rows == 1
    loaded = warehouse.read_daily_bars(symbols=["000001"], start_date="2026-06-18", end_date="2026-06-18")
    assert loaded["float_market_cap"].tolist() == [100.0]


def test_full_market_job_separates_imported_rows_from_filled_missing_rows(tmp_path):
    class TwoDayProvider(FakeProvider):
        def fetch_daily_bars(self, symbol, start_date, end_date):
            return pd.DataFrame(
                {
                    "symbol": [symbol, symbol],
                    "trade_date": ["2026-06-17", "2026-06-18"],
                    "open": [1.0, 1.1],
                    "high": [1.0, 1.1],
                    "low": [1.0, 1.1],
                    "close": [1.0, 1.1],
                    "volume": [1, 1],
                    "float_market_cap": [100.0, 110.0],
                    "total_market_cap": [120.0, 130.0],
                }
            )

    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001"],
                "trade_date": ["2026-06-17"],
                "open": [1.0],
                "high": [1.0],
                "low": [1.0],
                "close": [1.0],
                "volume": [1],
                "float_market_cap": [100.0],
                "total_market_cap": [120.0],
            }
        )
    )
    manager = SyncJobManager(warehouse=warehouse, provider=TwoDayProvider())

    status = manager.run_full_market(
        symbols=["000001"],
        start_date="2026-06-17",
        end_date="2026-06-18",
    )

    assert status.imported_rows == 2
    assert status.filled_missing_rows == 1
    assert status.filled_daily_rows == 1
    assert status.filled_market_cap_rows == 0
    loaded = warehouse.read_daily_bars(symbols=["000001"], start_date="2026-06-17", end_date="2026-06-18")
    assert loaded["trade_date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-06-17", "2026-06-18"]


def test_full_market_job_reports_daily_and_market_cap_fills_separately(tmp_path):
    class TwoSymbolProvider(FakeProvider):
        def fetch_daily_bars(self, symbol, start_date, end_date):
            return pd.DataFrame(
                {
                    "symbol": [symbol],
                    "trade_date": ["2026-06-18"],
                    "open": [1.0],
                    "high": [1.0],
                    "low": [1.0],
                    "close": [1.0],
                    "volume": [1],
                    "float_market_cap": [100.0],
                    "total_market_cap": [120.0],
                }
            )

    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000002"],
                "trade_date": ["2026-06-18"],
                "open": [2.0],
                "high": [2.0],
                "low": [2.0],
                "close": [2.0],
                "volume": [1],
                "float_market_cap": [float("nan")],
                "total_market_cap": [float("nan")],
            }
        )
    )
    manager = SyncJobManager(warehouse=warehouse, provider=TwoSymbolProvider())

    status = manager.run_full_market(
        symbols=["000001", "000002"],
        start_date="2026-06-18",
        end_date="2026-06-18",
    )

    assert status.imported_rows == 2
    assert status.filled_daily_rows == 1
    assert status.filled_market_cap_rows == 1
    assert status.filled_missing_rows == 2


def test_async_full_market_job_reuses_completeness_snapshot_for_gap_counts(tmp_path):
    class CountingWarehouse(Warehouse):
        def __init__(self, cache_root):
            super().__init__(cache_root)
            self.read_daily_calls = 0

        def read_daily_bars(self, *args, **kwargs):
            self.read_daily_calls += 1
            return super().read_daily_bars(*args, **kwargs)

    warehouse = CountingWarehouse(tmp_path)
    manager = SyncJobManager(
        warehouse=warehouse,
        provider=FakeProvider(),
        full_market_batch_size=1,
        full_market_workers=1,
        full_market_write_batch_rows=1,
    )

    status = manager.start_full_market(
        symbols=["000001", "000002", "000003"],
        start_date="2026-06-18",
        end_date="2026-06-18",
    )

    eventually = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        eventually = manager.get_job(status.job_id)
        if eventually and eventually.status == "completed":
            break
        sleep(0.02)

    assert eventually is not None
    assert eventually.status == "completed"
    assert eventually.imported_rows == 3
    assert eventually.filled_missing_rows == 3
    assert eventually.filled_daily_rows == 3
    assert eventually.filled_market_cap_rows == 0
    assert warehouse.read_daily_calls == 1


def test_incomplete_symbols_returns_gap_based_pool(tmp_path):
    """缺口基准补齐：只返回窗口内不完整的股票，完整股与退市股剔除。"""
    warehouse = Warehouse(tmp_path)
    rows = []
    for symbol, days in (("000001", ("2026-06-01", "2026-06-02", "2026-06-03")), ("000002", ("2026-06-01", "2026-06-03"))):
        for day in days:
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": day,
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "volume": 1000,
                    "float_market_cap": 100.0,
                    "total_market_cap": 120.0,
                }
            )
    warehouse.write_daily_bars(pd.DataFrame(rows))
    warehouse.upsert_symbol_lifecycle([{"symbol": "000004", "status": "delisted", "delisted_date": "2026-06-01"}])

    manager = SyncJobManager(warehouse=warehouse, provider=FakeProvider())
    incomplete = manager.incomplete_symbols("2026-06-01", "2026-06-03")

    # 000001 三天齐 → 完整剔除；000002 缺 06-02 → 保留；000004 已退市 → 剔除。
    assert incomplete == ["000002"]


def test_capital_flow_job_reports_completed_with_errors_when_rows_import_with_failures(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001"],
                "trade_date": ["2026-05-26"],
                "open": [10.0],
                "high": [10.5],
                "low": [9.8],
                "close": [10.2],
                "volume": [1000],
                "main_net_inflow": [float("nan")],
            }
        )
    )

    def fake_capital_flow_fetcher(symbols, start_date, end_date):
        return {
            "rows": [{"symbol": "000001", "trade_date": "2026-05-26", "main_net_inflow": 8800000.0}],
            "failures": [{"symbol": "000001", "code": "date_coverage_shortfall", "message": "partial range"}],
            "diagnostics": [],
        }

    manager = SyncJobManager(
        warehouse=warehouse,
        provider=FakeProvider(),
        cache=cache,
        capital_flow_fetcher=fake_capital_flow_fetcher,
    )

    status = manager.start_capital_flow_backfill(["000001"], "2026-05-26", "2026-05-29")

    eventually = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        eventually = manager.get_job(status.job_id)
        if eventually and eventually.status != "running":
            break
        sleep(0.02)

    assert eventually is not None
    assert eventually.status == "completed_with_errors"
    assert eventually.completed_symbols == 0
    assert eventually.failed_symbols == 1
    assert eventually.imported_rows == 1
    assert eventually.errors == ["000001: partial range"]


def test_capital_flow_job_counts_no_failure_zero_import_as_completed(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)

    def fake_capital_flow_fetcher(symbols, start_date, end_date):
        return {
            "rows": [],
            "failures": [],
            "diagnostics": [{"code": "capital_flow_backfill_not_needed"}],
        }

    manager = SyncJobManager(
        warehouse=warehouse,
        provider=FakeProvider(),
        cache=cache,
        capital_flow_fetcher=fake_capital_flow_fetcher,
    )

    status = manager.start_capital_flow_backfill(["000001"], "2026-05-26", "2026-05-29")

    eventually = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        eventually = manager.get_job(status.job_id)
        if eventually and eventually.status != "running":
            break
        sleep(0.02)

    assert eventually is not None
    assert eventually.status == "completed"
    assert eventually.completed_symbols == 1
    assert eventually.failed_symbols == 0
    assert eventually.imported_rows == 0
    assert eventually.errors == []


def test_capital_flow_job_treats_shortfall_diagnostics_as_retryable_failure(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)

    def fake_capital_flow_fetcher(symbols, start_date, end_date):
        return {
            "rows": [{"symbol": "000001", "trade_date": "2026-05-26", "main_net_inflow": 8800000.0}],
            "failures": [],
            "diagnostics": [
                {
                    "symbol": "000001",
                    "code": "date_coverage_shortfall",
                    "message": "returned 2026-05-26 to 2026-05-26 for requested 2026-05-26 to 2026-05-29",
                }
            ],
        }

    manager = SyncJobManager(
        warehouse=warehouse,
        provider=FakeProvider(),
        cache=cache,
        capital_flow_fetcher=fake_capital_flow_fetcher,
    )

    status = manager.start_capital_flow_backfill(["000001"], "2026-05-26", "2026-05-29")

    eventually = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        eventually = manager.get_job(status.job_id)
        if eventually and eventually.status != "running":
            break
        sleep(0.02)

    assert eventually is not None
    assert eventually.status == "completed_with_errors"
    assert eventually.processed_symbols == 1
    assert eventually.completed_symbols == 0
    assert eventually.failed_symbols == 1
    assert eventually.imported_rows == 1
    assert eventually.returned_rows == 1
    assert eventually.last_error is not None
    assert "000001" in eventually.last_error
    assert "date_coverage_shortfall" in eventually.last_error


def test_capital_flow_job_can_be_cancelled_between_batches(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)
    started = Event()
    release = Event()

    def fake_capital_flow_fetcher(symbols, start_date, end_date):
        started.set()
        release.wait(timeout=5)
        return {
            "rows": [
                {"symbol": symbols[0], "trade_date": "2026-06-05", "main_net_inflow": 1000000.0}
            ],
            "failures": [],
            "diagnostics": [],
        }

    manager = SyncJobManager(
        warehouse=warehouse,
        provider=FakeProvider(),
        cache=cache,
        capital_flow_fetcher=fake_capital_flow_fetcher,
        capital_flow_batch_size=1,
    )

    status = manager.start_capital_flow_backfill(["000001", "000002", "000003"], "2026-06-05", "2026-06-05")
    assert started.wait(timeout=5)
    cancelled = manager.cancel_job(status.job_id)
    assert cancelled is not None
    assert cancelled.status == "cancelling"
    release.set()

    eventually = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        eventually = manager.get_job(status.job_id)
        if eventually and eventually.status == "cancelled":
            break
        sleep(0.02)

    assert eventually is not None
    assert eventually.status == "cancelled"
    assert eventually.processed_symbols == 1
    assert eventually.completed_symbols == 1
    assert eventually.imported_rows == 1
    assert warehouse.read_daily_bars()["symbol"].tolist() == ["000001"]


def test_capital_flow_job_batches_symbols_and_accumulates_row_stats(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)
    calls = []

    def fake_capital_flow_fetcher(symbols, start_date, end_date):
        calls.append(list(symbols))
        rows = [
            {"symbol": symbol, "trade_date": "2026-06-05", "main_net_inflow": 1000000.0}
            for symbol in symbols
            if symbol != "000003"
        ]
        failures = (
            [{"symbol": "000003", "code": "network_error", "error": "remote disconnected"}]
            if "000003" in symbols
            else []
        )
        return {"rows": rows, "failures": failures, "diagnostics": failures}

    manager = SyncJobManager(
        warehouse=warehouse,
        provider=FakeProvider(),
        cache=cache,
        capital_flow_fetcher=fake_capital_flow_fetcher,
        capital_flow_batch_size=2,
    )

    status = manager.start_capital_flow_backfill(["000001", "000002", "000003"], "2026-06-05", "2026-06-05")

    eventually = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        eventually = manager.get_job(status.job_id)
        if eventually and eventually.status != "running":
            break
        sleep(0.02)

    assert eventually is not None
    assert eventually.status == "completed_with_errors"
    assert calls == [["000001", "000002"], ["000003"]]
    assert eventually.processed_symbols == 3
    assert eventually.completed_symbols == 2
    assert eventually.failed_symbols == 1
    assert eventually.returned_rows == 2
    assert eventually.imported_rows == 2
    assert eventually.last_error == "000003: remote disconnected"
    assert eventually.recent_failures[-1]["symbol"] == "000003"
