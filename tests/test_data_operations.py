from __future__ import annotations

import logging
import math

import pandas as pd
from astock_backtester.data import operations
from astock_backtester.data.cache import LocalCache
from astock_backtester.data.filelock import FileLockTimeout
from astock_backtester.data.importer import normalize_daily_bars
from astock_backtester.data.operations import (
    _read_existing_daily_bars,
    build_daily_bars_coverage,
    build_service_health,
    fetch_capital_flow_into_cache,
    fetch_daily_bars_into_cache,
    import_daily_bars_into_cache,
)
from astock_backtester.data.realtime_parsers import (
    aggregate_ths_hot_topics as _aggregate_ths_hot_topics,
)
from astock_backtester.data.trading_calendar import a_share_trade_dates
from astock_backtester.data.warehouse import Warehouse


def _bars(rows: list[tuple[object, ...]]) -> pd.DataFrame:
    frame = pd.DataFrame(
        rows,
        columns=[
            "symbol",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "turnover_rate",
            "float_market_cap",
            "main_net_inflow",
            "is_st",
            "is_suspended",
            "listing_days",
        ],
    )
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame


def test_cache_merge_preserves_existing_optional_values(tmp_path):
    cache = LocalCache(tmp_path)
    cache.write_daily_bars(
        _bars(
            [
                ("AAA", "2024-01-02", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, 1_500_000.0, False, False, 90),
                ("AAA", "2024-01-03", 10.5, 11.5, 10.0, 11.0, 1200, 0.1, 9_100_000_000.0, 1_600_000.0, False, False, 91),
            ]
        )
    )

    cache.write_daily_bars(
        _bars(
            [
                ("AAA", "2024-01-03", 20.0, 21.0, 19.0, 20.5, 2200, 0.2, float("nan"), float("nan"), False, False, 91),
                ("AAA", "2024-01-04", 21.0, 22.0, 20.0, 21.5, 2400, 0.2, 9_300_000_000.0, 1_800_000.0, False, False, 92),
            ]
        )
    )

    loaded = cache.read_daily_bars()

    assert loaded["trade_date"].dt.strftime("%Y-%m-%d").tolist() == ["2024-01-02", "2024-01-03", "2024-01-04"]
    merged = loaded.loc[loaded["trade_date"] == pd.Timestamp("2024-01-03")].iloc[0]
    assert merged["open"] == 20.0
    assert merged["close"] == 20.5
    assert merged["float_market_cap"] == 9_100_000_000.0
    assert merged["main_net_inflow"] == 1_600_000.0


def test_read_existing_daily_bars_logs_warehouse_failure_before_cache_fallback(tmp_path, caplog):
    class BrokenWarehouse:
        def read_daily_bars(self, **_kwargs):
            raise OSError("corrupt warehouse partition")

    cache = LocalCache(tmp_path)
    cache.write_daily_bars(
        _bars(
            [
                (
                    "AAA",
                    "2026-06-18",
                    10.0,
                    11.0,
                    9.0,
                    10.5,
                    1000,
                    0.1,
                    9_000_000_000.0,
                    1.0,
                    False,
                    False,
                    90,
                ),
            ]
        )
    )

    with caplog.at_level(logging.WARNING, logger="astock_backtester.data.operations"):
        result = _read_existing_daily_bars(
            cache,
            BrokenWarehouse(),
            symbols=["AAA"],
            start_date="2026-06-18",
            end_date="2026-06-18",
        )

    assert result["symbol"].tolist() == ["AAA"]
    assert "warehouse daily-bars read failed" in caplog.text


def test_importer_keeps_missing_capital_flow_as_missing():
    result = normalize_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["AAA"],
                "trade_date": ["2024-01-02"],
                "open": [10.0],
                "high": [11.0],
                "low": [9.0],
                "close": [10.5],
                "volume": [1000],
            }
        )
    )

    assert math.isnan(result.loc[0, "main_net_inflow"])


def test_coverage_reports_missing_ranges_and_optional_field_gaps(tmp_path):
    cache = LocalCache(tmp_path)
    cache.write_daily_bars(
        _bars(
            [
                ("AAA", "2024-01-02", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, 1_500_000.0, False, False, 90),
                ("AAA", "2024-01-04", 10.7, 11.7, 9.7, 11.1, 1100, 0.1, float("nan"), float("nan"), False, False, 92),
                ("BBB", "2024-01-03", 20.0, 21.0, 19.0, 20.5, 900, 0.2, 20_000_000_000.0, float("nan"), False, False, 120),
            ]
        )
    )

    datasets = {item.dataset: item for item in cache.coverage()}
    details = build_daily_bars_coverage(cache)

    assert set(datasets) == {"daily_bars", "capital_flow", "market_cap"}
    assert datasets["capital_flow"].missing_rows == 2
    assert datasets["market_cap"].missing_rows == 1

    aaa = next(item for item in details.items if item.symbol == "AAA")
    assert aaa.start_date.isoformat() == "2024-01-02"
    assert aaa.end_date.isoformat() == "2024-01-04"
    assert [day.isoformat() for day in aaa.missing_trade_dates] == ["2024-01-03"]
    assert [day.isoformat() for day in aaa.missing_capital_flow_dates] == ["2024-01-04"]
    assert [day.isoformat() for day in aaa.missing_market_cap_dates] == ["2024-01-04"]


def test_coverage_filters_symbols_dates_and_ignores_weekends(tmp_path):
    cache = LocalCache(tmp_path)
    cache.write_daily_bars(
        _bars(
            [
                ("AAA", "2024-01-02", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, 1_500_000.0, False, False, 90),
                ("AAA", "2024-01-05", 10.5, 11.5, 10.0, 11.0, 1200, 0.1, 9_100_000_000.0, 1_600_000.0, False, False, 93),
                ("BBB", "2024-01-03", 20.0, 21.0, 19.0, 20.5, 900, 0.2, 20_000_000_000.0, 1_000_000.0, False, False, 120),
            ]
        )
    )

    details = build_daily_bars_coverage(
        cache,
        symbols=["AAA"],
        start_date="2024-01-02",
        end_date="2024-01-08",
    )

    assert [item.symbol for item in details.items] == ["AAA"]
    aaa = details.items[0]
    assert aaa.start_date.isoformat() == "2024-01-02"
    assert aaa.end_date.isoformat() == "2024-01-05"
    assert aaa.rows == 2
    assert [day.isoformat() for day in aaa.missing_trade_dates] == ["2024-01-03", "2024-01-04", "2024-01-08"]


def test_coverage_uses_a_share_trading_calendar_for_2026_holidays(tmp_path):
    cache = LocalCache(tmp_path)
    cache.write_daily_bars(
        _bars(
            [
                ("AAA", "2026-02-13", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, 1_500_000.0, False, False, 90),
                ("AAA", "2026-02-23", 10.5, 11.5, 10.0, 11.0, 1200, 0.1, 9_100_000_000.0, 1_600_000.0, False, False, 91),
                ("AAA", "2026-04-03", 11.0, 11.8, 10.8, 11.4, 1300, 0.1, 9_200_000_000.0, 1_700_000.0, False, False, 92),
                ("AAA", "2026-04-07", 11.4, 12.0, 11.2, 11.7, 1400, 0.1, 9_300_000_000.0, 1_800_000.0, False, False, 93),
                ("AAA", "2026-04-30", 11.7, 12.2, 11.4, 11.9, 1500, 0.1, 9_400_000_000.0, 1_900_000.0, False, False, 94),
                ("AAA", "2026-05-06", 11.9, 12.4, 11.6, 12.1, 1600, 0.1, 9_500_000_000.0, 2_000_000.0, False, False, 95),
            ]
        )
    )

    details = build_daily_bars_coverage(
        cache=cache,
        symbols=["AAA"],
        start_date="2026-02-13",
        end_date="2026-05-06",
    )

    missing = {day.isoformat() for day in details.items[0].missing_trade_dates}
    assert not (missing & {"2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20"})
    assert "2026-04-06" not in missing
    assert not (missing & {"2026-05-01", "2026-05-04", "2026-05-05"})


def test_coverage_reports_requested_range_edge_gaps_without_holiday_false_positives(tmp_path):
    cache = LocalCache(tmp_path)
    cache.write_daily_bars(
        _bars(
            [
                ("AAA", "2026-04-07", 11.4, 12.0, 11.2, 11.7, 1400, 0.1, 9_300_000_000.0, 1_800_000.0, False, False, 93),
            ]
        )
    )

    details = build_daily_bars_coverage(
        cache=cache,
        symbols=["AAA"],
        start_date="2026-04-01",
        end_date="2026-04-07",
    )

    missing = [day.isoformat() for day in details.items[0].missing_trade_dates]
    assert missing == ["2026-04-01", "2026-04-02", "2026-04-03"]
    assert "2026-04-06" not in missing


def test_coverage_uses_a_share_trading_calendar_for_2024_holidays(tmp_path):
    cache = LocalCache(tmp_path)
    cache.write_daily_bars(
        _bars(
            [
                ("AAA", "2024-02-08", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, 1_500_000.0, False, False, 90),
                ("AAA", "2024-02-19", 10.5, 11.5, 10.0, 11.0, 1200, 0.1, 9_100_000_000.0, 1_600_000.0, False, False, 91),
                ("AAA", "2024-04-03", 11.0, 11.8, 10.8, 11.4, 1300, 0.1, 9_200_000_000.0, 1_700_000.0, False, False, 92),
                ("AAA", "2024-04-08", 11.4, 12.0, 11.2, 11.7, 1400, 0.1, 9_300_000_000.0, 1_800_000.0, False, False, 93),
                ("AAA", "2024-04-30", 11.7, 12.2, 11.4, 11.9, 1500, 0.1, 9_400_000_000.0, 1_900_000.0, False, False, 94),
                ("AAA", "2024-05-06", 11.9, 12.4, 11.6, 12.1, 1600, 0.1, 9_500_000_000.0, 2_000_000.0, False, False, 95),
                ("AAA", "2024-09-14", 12.1, 12.4, 11.9, 12.2, 1600, 0.1, 9_600_000_000.0, 2_100_000.0, False, False, 96),
                ("AAA", "2024-09-18", 12.2, 12.5, 12.0, 12.3, 1600, 0.1, 9_700_000_000.0, 2_200_000.0, False, False, 97),
                ("AAA", "2024-09-30", 12.3, 12.6, 12.1, 12.4, 1600, 0.1, 9_800_000_000.0, 2_300_000.0, False, False, 98),
                ("AAA", "2024-10-08", 12.4, 12.7, 12.2, 12.5, 1600, 0.1, 9_900_000_000.0, 2_400_000.0, False, False, 99),
            ]
        )
    )

    details = build_daily_bars_coverage(
        cache=cache,
        symbols=["AAA"],
        start_date="2024-02-08",
        end_date="2024-10-08",
    )

    missing = {day.isoformat() for day in details.items[0].missing_trade_dates}
    assert not (missing & {"2024-02-09", "2024-02-12", "2024-02-13", "2024-02-14", "2024-02-15", "2024-02-16"})
    assert not (missing & {"2024-04-04", "2024-04-05"})
    assert not (missing & {"2024-05-01", "2024-05-02", "2024-05-03"})
    assert not (missing & {"2024-09-16", "2024-09-17"})
    assert not (missing & {"2024-10-01", "2024-10-02", "2024-10-03", "2024-10-04", "2024-10-07"})


def test_coverage_uses_a_share_trading_calendar_for_2025_holidays(tmp_path):
    cache = LocalCache(tmp_path)
    cache.write_daily_bars(
        _bars(
            [
                ("AAA", "2025-01-27", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, 1_500_000.0, False, False, 90),
                ("AAA", "2025-02-05", 10.5, 11.5, 10.0, 11.0, 1200, 0.1, 9_100_000_000.0, 1_600_000.0, False, False, 91),
                ("AAA", "2025-04-03", 11.0, 11.8, 10.8, 11.4, 1300, 0.1, 9_200_000_000.0, 1_700_000.0, False, False, 92),
                ("AAA", "2025-04-07", 11.4, 12.0, 11.2, 11.7, 1400, 0.1, 9_300_000_000.0, 1_800_000.0, False, False, 93),
                ("AAA", "2025-04-30", 11.7, 12.2, 11.4, 11.9, 1500, 0.1, 9_400_000_000.0, 1_900_000.0, False, False, 94),
                ("AAA", "2025-05-06", 11.9, 12.4, 11.6, 12.1, 1600, 0.1, 9_500_000_000.0, 2_000_000.0, False, False, 95),
                ("AAA", "2025-09-30", 12.1, 12.4, 11.9, 12.2, 1600, 0.1, 9_600_000_000.0, 2_100_000.0, False, False, 96),
                ("AAA", "2025-10-09", 12.2, 12.5, 12.0, 12.3, 1600, 0.1, 9_700_000_000.0, 2_200_000.0, False, False, 97),
            ]
        )
    )

    details = build_daily_bars_coverage(
        cache=cache,
        symbols=["AAA"],
        start_date="2025-01-27",
        end_date="2025-10-09",
    )

    missing = {day.isoformat() for day in details.items[0].missing_trade_dates}
    assert not (missing & {"2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31", "2025-02-03", "2025-02-04"})
    assert not (missing & {"2025-04-04"})
    assert not (missing & {"2025-05-01", "2025-05-02", "2025-05-05"})
    assert not (missing & {"2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06", "2025-10-07", "2025-10-08"})


def test_fetch_result_reports_partial_success(tmp_path):
    cache = LocalCache(tmp_path)

    def fake_fetcher(symbols: list[str], start_date: str, end_date: str) -> pd.DataFrame:
        assert symbols == ["AAA", "BBB"]
        assert start_date == "2024-01-02"
        assert end_date == "2024-01-03"
        return _bars(
            [
                ("AAA", "2024-01-02", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, 1_500_000.0, False, False, 90),
                ("AAA", "2024-01-03", 10.5, 11.5, 10.0, 11.0, 1200, 0.1, 9_100_000_000.0, float("nan"), False, False, 91),
            ]
        )

    result = fetch_daily_bars_into_cache(
        cache=cache,
        fetcher=fake_fetcher,
        symbols=["AAA", "BBB"],
        start_date="2024-01-02",
        end_date="2024-01-03",
    )

    assert result.status == "partial"
    assert result.requested_symbols == ["AAA", "BBB"]
    assert result.fetched_symbols == ["AAA"]
    assert result.missing_symbols == ["BBB"]
    assert result.imported_rows == 2
    assert any(entry.level == "warning" and "BBB" in entry.message for entry in result.logs)


def test_fetch_daily_bars_uses_capital_flow_crawler_as_primary_inflow_source(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)

    def fake_fetcher(symbols: list[str], start_date: str, end_date: str) -> pd.DataFrame:
        assert symbols == ["AAA"]
        return _bars(
            [
                ("AAA", "2024-01-02", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, 10.0, False, False, 90),
                ("AAA", "2024-01-03", 10.5, 11.5, 10.0, 11.0, 1200, 0.1, 9_100_000_000.0, float("nan"), False, False, 91),
            ]
        )

    def fake_capital_flow_fetcher(symbols: list[str], start_date: str, end_date: str) -> dict:
        assert symbols == ["AAA"]
        assert start_date == "2024-01-02"
        assert end_date == "2024-01-03"
        return {
            "rows": [
                {"symbol": "AAA", "trade_date": "2024-01-02", "main_net_inflow": 1_500_000.0},
                {"symbol": "AAA", "trade_date": "2024-01-03", "main_net_inflow": -2_000_000.0},
            ],
            "failures": [],
            "diagnostics": [],
        }

    result = fetch_daily_bars_into_cache(
        cache=cache,
        warehouse=warehouse,
        fetcher=fake_fetcher,
        capital_flow_fetcher=fake_capital_flow_fetcher,
        symbols=["AAA"],
        start_date="2024-01-02",
        end_date="2024-01-03",
    )

    cached = cache.read_daily_bars()
    stored = warehouse.read_daily_bars(symbols=["AAA"])

    assert result.status == "ok"
    assert result.imported_rows == 2
    assert cached["main_net_inflow"].tolist() == [1_500_000.0, -2_000_000.0]
    assert stored["main_net_inflow"].tolist() == [1_500_000.0, -2_000_000.0]
    assert any("Capital-flow crawler merged 2 rows" in entry.message for entry in result.logs)
    assert result.diagnostics == [
        {
            "code": "capital_flow_crawler_merge",
            "requested_symbols": 1,
            "merged_rows": 2,
            "source": "capital_flow_crawler",
        }
    ]


def test_fetch_daily_bars_reports_partial_when_capital_flow_crawler_merges_no_rows(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)

    def fake_fetcher(symbols: list[str], start_date: str, end_date: str) -> pd.DataFrame:
        return _bars(
            [
                ("AAA", "2024-01-02", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, float("nan"), False, False, 90),
            ]
        )

    def fake_capital_flow_fetcher(symbols: list[str], start_date: str, end_date: str) -> dict:
        return {
            "rows": [],
            "failures": [],
            "diagnostics": [{"symbol": "AAA", "code": "date_coverage_shortfall", "message": "no rows in range"}],
        }

    result = fetch_daily_bars_into_cache(
        cache=cache,
        warehouse=warehouse,
        fetcher=fake_fetcher,
        capital_flow_fetcher=fake_capital_flow_fetcher,
        symbols=["AAA"],
        start_date="2024-01-02",
        end_date="2024-01-03",
    )

    assert result.status == "partial"
    assert result.missing_symbols == ["AAA"]
    assert any(item["code"] == "capital_flow_crawler_zero_merge" for item in result.diagnostics)
    assert any(item["code"] == "capital_flow_crawler_unfilled_main_net_inflow" for item in result.diagnostics)
    assert any(entry.level == "warning" and "merged 0 rows" in entry.message for entry in result.logs)


def test_fetch_capital_flow_into_cache_backfills_existing_daily_rows_only(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)
    initial = _bars(
        [
            ("AAA", "2024-01-02", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, float("nan"), False, False, 90),
            ("AAA", "2024-01-03", 10.5, 11.5, 10.0, 11.0, 1200, 0.1, 9_100_000_000.0, float("nan"), False, False, 91),
            ("BBB", "2024-01-02", 20.0, 21.0, 19.0, 20.5, 900, 0.2, 20_000_000_000.0, float("nan"), False, False, 120),
        ]
    )
    cache.write_daily_bars(initial)
    warehouse.write_daily_bars(initial)

    def fake_capital_flow_fetcher(symbols: list[str], start_date: str, end_date: str) -> dict:
        assert symbols == ["AAA", "BBB"]
        return {
            "rows": [
                {"symbol": "AAA", "trade_date": "2024-01-02", "main_net_inflow": 1_500_000.0},
                {"symbol": "AAA", "trade_date": "2024-01-03", "main_net_inflow": -2_000_000.0},
                {"symbol": "AAA", "trade_date": "2024-01-04", "main_net_inflow": 9_999_999.0},
            ],
            "failures": [{"symbol": "BBB", "code": "network_error", "error": "remote disconnected"}],
            "diagnostics": [{"symbol": "BBB", "code": "network_error", "message": "remote disconnected"}],
        }

    result = fetch_capital_flow_into_cache(
        cache=cache,
        warehouse=warehouse,
        capital_flow_fetcher=fake_capital_flow_fetcher,
        symbols=["AAA", "BBB"],
        start_date="2024-01-02",
        end_date="2024-01-03",
    )

    stored = warehouse.read_daily_bars(symbols=["AAA", "BBB"])
    aaa = stored.loc[stored["symbol"] == "AAA"].sort_values("trade_date")
    bbb = stored.loc[stored["symbol"] == "BBB"].iloc[0]

    assert result.status == "partial"
    assert result.imported_rows == 2
    assert result.requested_symbols == ["AAA", "BBB"]
    assert result.fetched_symbols == ["AAA"]
    assert result.missing_symbols == ["BBB"]
    assert aaa["main_net_inflow"].tolist() == [1_500_000.0, -2_000_000.0]
    assert math.isnan(bbb["main_net_inflow"])
    assert result.failures == [{"symbol": "BBB", "code": "network_error", "error": "remote disconnected"}]
    assert any(item["code"] == "network_error" for item in result.diagnostics)
    assert any("Capital-flow crawler merged 2 rows" in entry.message for entry in result.logs)


def test_fetch_capital_flow_into_cache_writes_standalone_rows_without_daily_rows(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)

    def fake_capital_flow_fetcher(symbols: list[str], start_date: str, end_date: str) -> dict:
        assert symbols == ["AAA", "BBB"]
        assert start_date == "2024-01-02"
        assert end_date == "2024-01-03"
        return {
            "rows": [
                {"symbol": "AAA", "trade_date": "2024-01-02", "main_net_inflow": 1_500_000.0},
                {"symbol": "AAA", "trade_date": "2024-01-03", "main_net_inflow": -2_000_000.0},
            ],
            "failures": [],
            "diagnostics": [],
        }

    result = fetch_capital_flow_into_cache(
        cache=cache,
        warehouse=warehouse,
        capital_flow_fetcher=fake_capital_flow_fetcher,
        symbols=["AAA", "BBB"],
        start_date="2024-01-02",
        end_date="2024-01-03",
    )

    stored = warehouse.read_daily_bars(symbols=["AAA"])
    datasets = {item.dataset: item for item in result.coverage}

    assert result.status == "partial"
    assert result.imported_rows == 2
    assert result.fetched_symbols == ["AAA"]
    assert result.missing_symbols == ["BBB"]
    assert stored["main_net_inflow"].tolist() == [1_500_000.0, -2_000_000.0]
    assert stored[["open", "high", "low", "close"]].isna().all().all()
    assert datasets["capital_flow"].symbols == 1
    assert datasets["daily_bars"].symbols == 0
    assert datasets["daily_bars"].missing_rows == 0
    assert any(item["code"] == "capital_flow_crawler_standalone_rows" for item in result.diagnostics)


def test_fetch_capital_flow_into_cache_marks_shortfall_diagnostics_as_partial(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)

    def fake_capital_flow_fetcher(symbols: list[str], start_date: str, end_date: str) -> dict:
        return {
            "rows": [{"symbol": "AAA", "trade_date": "2024-01-02", "main_net_inflow": 1_500_000.0}],
            "failures": [],
            "diagnostics": [
                {
                    "symbol": "AAA",
                    "code": "date_coverage_shortfall",
                    "message": "returned 2024-01-02 to 2024-01-02 for requested 2024-01-02 to 2024-01-03",
                }
            ],
        }

    result = fetch_capital_flow_into_cache(
        cache=cache,
        warehouse=warehouse,
        capital_flow_fetcher=fake_capital_flow_fetcher,
        symbols=["AAA"],
        start_date="2024-01-02",
        end_date="2024-01-03",
    )

    assert result.status == "partial"
    assert result.imported_rows == 1
    assert result.returned_rows == 1
    assert result.missing_symbols == ["AAA"]
    assert result.failures == [
        {
            "symbol": "AAA",
            "code": "date_coverage_shortfall",
            "error": "date_coverage_shortfall: returned 2024-01-02 to 2024-01-02 for requested 2024-01-02 to 2024-01-03",
        }
    ]


def test_fetch_capital_flow_into_cache_clips_requested_range_to_a_share_trade_dates(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        _bars(
            [
                ("AAA", "2026-06-18", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, float("nan"), False, False, 90),
            ]
        )
    )
    calls = []

    def fake_capital_flow_fetcher(symbols: list[str], start_date: str, end_date: str) -> dict:
        calls.append((symbols, start_date, end_date))
        return {
            "rows": [{"symbol": "AAA", "trade_date": "2026-06-18", "main_net_inflow": 1_500_000.0}],
            "failures": [],
            "diagnostics": [],
        }

    result = fetch_capital_flow_into_cache(
        cache=cache,
        warehouse=warehouse,
        capital_flow_fetcher=fake_capital_flow_fetcher,
        symbols=["AAA"],
        start_date="2026-06-18",
        end_date="2026-06-19",
    )

    assert calls == [(["AAA"], "2026-06-18", "2026-06-18")]
    assert result.status == "ok"
    assert result.missing_symbols == []
    assert result.failures == []


def test_fetch_capital_flow_into_cache_allows_standalone_shortfall_without_daily_gap(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)

    def fake_capital_flow_fetcher(symbols: list[str], start_date: str, end_date: str) -> dict:
        return {
            "rows": [
                {"symbol": "AAA", "trade_date": "2024-01-02", "main_net_inflow": 1_500_000.0},
                {"symbol": "AAA", "trade_date": "2024-01-03", "main_net_inflow": -2_000_000.0},
            ],
            "failures": [],
            "diagnostics": [
                {
                    "symbol": "AAA",
                    "code": "date_coverage_shortfall",
                    "provider": "sina",
                    "message": "returned 2024-01-02 to 2024-01-03 for requested 2015-01-01 to 2024-01-03",
                }
            ],
        }

    result = fetch_capital_flow_into_cache(
        cache=cache,
        warehouse=warehouse,
        capital_flow_fetcher=fake_capital_flow_fetcher,
        symbols=["AAA"],
        start_date="2015-01-01",
        end_date="2024-01-03",
    )

    assert result.status == "ok"
    assert result.imported_rows == 2
    assert result.fetched_symbols == ["AAA"]
    assert result.missing_symbols == []
    assert result.failures == []
    assert any(item["code"] == "date_coverage_shortfall" for item in result.diagnostics)


def test_fetch_capital_flow_into_cache_allows_sina_common_gaps_after_large_import(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        _bars(
            [
                ("AAA", "2024-01-02", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, float("nan"), False, False, 90),
                ("AAA", "2024-01-03", 10.2, 11.1, 9.8, 10.7, 1000, 0.1, 9_000_000_000.0, float("nan"), False, False, 91),
            ]
        )
    )

    def fake_capital_flow_fetcher(symbols: list[str], start_date: str, end_date: str) -> dict:
        return {
            "rows": [{"symbol": "AAA", "trade_date": "2024-01-02", "main_net_inflow": 1_500_000.0}],
            "failures": [],
            "diagnostics": [
                {
                    "symbol": "AAA",
                    "code": "provider_fallback_used",
                    "provider": "sina",
                    "rows": 1,
                },
                {
                    "symbol": "AAA",
                    "code": "date_coverage_shortfall",
                    "provider": "sina",
                    "message": "provider has a known common missing date",
                },
            ],
        }

    result = fetch_capital_flow_into_cache(
        cache=cache,
        warehouse=warehouse,
        capital_flow_fetcher=fake_capital_flow_fetcher,
        symbols=["AAA"],
        start_date="2024-01-02",
        end_date="2024-01-03",
    )

    assert result.status == "ok"
    assert result.imported_rows == 1
    assert result.fetched_symbols == ["AAA"]
    assert result.missing_symbols == []
    assert result.failures == []
    assert any(item["code"] == "date_coverage_shortfall" for item in result.diagnostics)


def test_fetch_capital_flow_into_cache_marks_known_source_gap_remaining(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        _bars(
            [
                ("AAA", "2019-04-04", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, float("nan"), False, False, 90),
                ("AAA", "2019-04-08", 10.5, 11.5, 10.0, 11.0, 1200, 0.1, 9_100_000_000.0, float("nan"), False, False, 91),
            ]
        )
    )

    def fake_capital_flow_fetcher(symbols: list[str], start_date: str, end_date: str) -> dict:
        return {
            "rows": [{"symbol": "AAA", "trade_date": "2019-04-08", "main_net_inflow": 1_500_000.0}],
            "failures": [],
            "diagnostics": [],
        }

    result = fetch_capital_flow_into_cache(
        cache=cache,
        warehouse=warehouse,
        capital_flow_fetcher=fake_capital_flow_fetcher,
        symbols=["AAA"],
        start_date="2019-04-04",
        end_date="2019-04-08",
    )

    assert result.status == "ok"
    assert result.missing_symbols == []
    assert result.failures == []
    assert any(
        item["code"] == "capital_flow_known_source_gap_remaining" and item["symbol"] == "AAA"
        for item in result.diagnostics
    )


def test_fetch_capital_flow_into_cache_skips_listing_lag_source_start_gap(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        _bars(
            [
                ("603027", "2016-03-07", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, float("nan"), False, False, 1),
                ("603027", "2016-03-08", 10.5, 11.5, 10.0, 11.0, 1200, 0.1, 9_100_000_000.0, 1_500_000.0, False, False, 2),
            ]
        )
    )

    calls: list[list[str]] = []

    def fake_capital_flow_fetcher(symbols: list[str], start_date: str, end_date: str) -> dict:
        calls.append(symbols)
        return {"rows": [], "failures": [], "diagnostics": []}

    result = fetch_capital_flow_into_cache(
        cache=cache,
        warehouse=warehouse,
        capital_flow_fetcher=fake_capital_flow_fetcher,
        symbols=["603027"],
        start_date="2016-03-07",
        end_date="2016-03-08",
    )

    assert result.status == "ok"
    assert calls == []
    assert result.skipped_symbols == ["603027"]
    assert any(item["code"] == "capital_flow_backfill_not_needed" for item in result.diagnostics)


def test_fetch_capital_flow_into_cache_keeps_missing_daily_symbols_when_other_symbols_backfill(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        _bars(
            [
                ("AAA", "2024-01-02", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, float("nan"), False, False, 90),
            ]
        )
    )

    def fake_capital_flow_fetcher(symbols: list[str], start_date: str, end_date: str) -> dict:
        assert symbols == ["AAA", "BBB"]
        return {
            "rows": [{"symbol": "AAA", "trade_date": "2024-01-02", "main_net_inflow": 1_500_000.0}],
            "failures": [],
            "diagnostics": [],
        }

    result = fetch_capital_flow_into_cache(
        cache=cache,
        warehouse=warehouse,
        capital_flow_fetcher=fake_capital_flow_fetcher,
        symbols=["AAA", "BBB"],
        start_date="2024-01-02",
        end_date="2024-01-02",
    )

    assert result.status == "partial"
    assert result.imported_rows == 1
    assert result.fetched_symbols == ["AAA"]
    assert result.missing_symbols == ["BBB"]
    assert any(
        item["code"] == "capital_flow_crawler_merge" and item["requested_symbols"] == 2
        for item in result.diagnostics
    )


def test_fetch_capital_flow_into_cache_reports_returned_rows_and_skipped_complete_symbols(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)
    initial = _bars(
        [
            ("AAA", "2024-01-02", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, 1_000_000.0, False, False, 90),
            ("BBB", "2024-01-02", 20.0, 21.0, 19.0, 20.5, 900, 0.2, 20_000_000_000.0, float("nan"), False, False, 120),
        ]
    )
    warehouse.write_daily_bars(initial)

    def fake_capital_flow_fetcher(symbols: list[str], start_date: str, end_date: str) -> dict:
        assert symbols == ["BBB"]
        return {
            "rows": [
                {"symbol": "BBB", "trade_date": "2024-01-02", "main_net_inflow": 2_500_000.0},
                {"symbol": "BBB", "trade_date": "2024-01-03", "main_net_inflow": 2_600_000.0},
            ],
            "failures": [],
            "diagnostics": [],
        }

    result = fetch_capital_flow_into_cache(
        cache=cache,
        warehouse=warehouse,
        capital_flow_fetcher=fake_capital_flow_fetcher,
        symbols=["AAA", "BBB"],
        start_date="2024-01-02",
        end_date="2024-01-02",
    )

    assert result.imported_rows == 1
    assert any(
        item["code"] == "capital_flow_crawler_fetch_summary"
        and item["requested_symbols"] == 1
        and item["returned_rows"] == 2
        and item["skipped_symbols"] == ["AAA"]
        for item in result.diagnostics
    )
    assert any(
        item["code"] == "capital_flow_symbol_summary"
        and item["symbol"] == "BBB"
        and item["returned_rows"] == 2
        and item["imported_rows"] == 1
        for item in result.diagnostics
    )


def test_fetch_capital_flow_into_cache_marks_returned_existing_rows_as_processed(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)
    initial = _bars(
        [
            ("AAA", "2024-01-02", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, 1_000_000.0, False, False, 90),
            ("AAA", "2024-01-03", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, float("nan"), False, False, 91),
        ]
    )
    warehouse.write_daily_bars(initial)

    def fake_capital_flow_fetcher(symbols: list[str], start_date: str, end_date: str) -> dict:
        return {
            "rows": [{"symbol": "AAA", "trade_date": "2024-01-02", "main_net_inflow": 1_000_000.0}],
            "failures": [],
            "diagnostics": [
                {
                    "symbol": "AAA",
                    "code": "provider_fallback_used",
                    "provider": "sina",
                    "rows": 1,
                }
            ],
        }

    result = fetch_capital_flow_into_cache(
        cache=cache,
        warehouse=warehouse,
        capital_flow_fetcher=fake_capital_flow_fetcher,
        symbols=["AAA"],
        start_date="2024-01-02",
        end_date="2024-01-03",
    )

    assert result.status == "ok"
    assert result.imported_rows == 0
    assert result.returned_rows == 1
    assert result.fetched_symbols == ["AAA"]
    assert result.missing_symbols == []


def test_fetch_capital_flow_into_cache_can_defer_expensive_coverage_refresh(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)

    def fake_capital_flow_fetcher(symbols: list[str], start_date: str, end_date: str) -> dict:
        return {
            "rows": [{"symbol": "AAA", "trade_date": "2024-01-02", "main_net_inflow": 1_000_000.0}],
            "failures": [],
            "diagnostics": [],
        }

    def fail_if_called():
        raise AssertionError("coverage should be deferred during batch backfill")

    warehouse.coverage = fail_if_called  # type: ignore[method-assign]
    cache.coverage = fail_if_called  # type: ignore[method-assign]

    result = fetch_capital_flow_into_cache(
        cache=cache,
        warehouse=warehouse,
        capital_flow_fetcher=fake_capital_flow_fetcher,
        symbols=["AAA"],
        start_date="2024-01-02",
        end_date="2024-01-02",
        refresh_coverage=False,
    )

    assert result.status == "ok"
    assert result.imported_rows == 1
    assert result.coverage == []


def test_health_payload_includes_cache_path_and_port(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)
    result = import_daily_bars_into_cache(
        cache=cache,
        frame=_bars(
            [
                ("AAA", "2024-01-02", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, 1_500_000.0, False, False, 90),
            ]
        ),
        source="unit-test",
    )

    health = build_service_health(
        cache=cache,
        warehouse=warehouse,
        port=8765,
        process_id=1234,
        executable_path="D:\\New project 6\\src-tauri\\bin\\astock-data-service.exe",
        executable_sha256="abc123",
        started_at="2026-06-08T01:02:03+00:00",
        instance_id="instance-1",
    )

    assert result.imported_rows == 1
    assert health.port == 8765
    assert health.cache_path == str(cache.root.resolve())
    assert health.process_id == 1234
    assert health.executable_path.endswith("astock-data-service.exe")
    assert health.executable_sha256 == "abc123"
    assert health.started_at is not None
    assert health.instance_id == "instance-1"
    assert [item.dataset for item in health.coverage] == ["daily_bars", "capital_flow", "market_cap"]


def test_health_prefers_warehouse_coverage_when_available(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        _bars(
            [
                ("AAA", "2024-01-02", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, float("nan"), False, False, 90),
                ("AAA", "2024-01-03", 10.5, 11.5, 10.0, 11.0, 1200, 0.1, 9_100_000_000.0, 1_500_000.0, False, False, 91),
            ]
        )
    )

    health = build_service_health(cache=cache, warehouse=warehouse, port=8765)
    datasets = {item.dataset: item for item in health.coverage}

    assert datasets["daily_bars"].symbols == 1
    assert datasets["market_cap"].missing_rows == 0
    assert datasets["capital_flow"].missing_rows == 1


def test_health_falls_back_to_cache_when_warehouse_coverage_fails(tmp_path):
    class BrokenWarehouse:
        def coverage(self):
            raise RuntimeError("bad warehouse partition")

    cache = LocalCache(tmp_path)
    cache.write_daily_bars(
        _bars(
            [
                ("AAA", "2024-01-02", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, 1_500_000.0, False, False, 90),
            ]
        )
    )

    health = build_service_health(cache=cache, warehouse=BrokenWarehouse(), port=8765)
    datasets = {item.dataset: item for item in health.coverage}

    assert health.ok is True
    assert datasets["daily_bars"].symbols == 1


def test_daily_bars_coverage_falls_back_to_cache_when_warehouse_read_fails(tmp_path):
    class BrokenWarehouse:
        def read_daily_bars(self, **kwargs):
            raise RuntimeError("bad warehouse parquet")

    cache = LocalCache(tmp_path)
    cache.write_daily_bars(
        _bars(
            [
                ("AAA", "2026-05-26", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, 1_500_000.0, False, False, 90),
                ("AAA", "2026-06-05", 10.5, 11.5, 10.0, 11.0, 1200, 0.1, 9_100_000_000.0, 1_600_000.0, False, False, 91),
            ]
        )
    )

    details = build_daily_bars_coverage(
        cache=cache,
        warehouse=BrokenWarehouse(),
        symbols=["AAA"],
        start_date="2026-05-26",
        end_date="2026-06-05",
    )

    assert [item.symbol for item in details.items] == ["AAA"]
    assert details.items[0].end_date.isoformat() == "2026-06-05"


def test_coverage_prefers_warehouse_rows_when_available(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        _bars(
            [
                ("AAA", "2024-01-02", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, 1_500_000.0, False, False, 90),
                ("AAA", "2024-01-03", 10.5, 11.5, 10.0, 11.0, 1200, 0.1, 9_100_000_000.0, 1_600_000.0, False, False, 91),
            ]
        )
    )

    details = build_daily_bars_coverage(
        cache=cache,
        warehouse=warehouse,
        symbols=["AAA"],
        start_date="2024-01-02",
        end_date="2024-01-03",
    )

    assert [item.symbol for item in details.items] == ["AAA"]
    assert details.items[0].rows == 2


def test_import_syncs_warehouse_when_provided(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)

    result = import_daily_bars_into_cache(
        cache=cache,
        warehouse=warehouse,
        frame=_bars(
            [
                ("AAA", "2024-01-02", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, 1_500_000.0, False, False, 90),
            ]
        ),
        source="unit-test",
    )

    cached = cache.read_daily_bars()
    stored = warehouse.read_daily_bars()
    datasets = {item.dataset: item for item in result.coverage}

    assert len(cached) == 1
    assert len(stored) == 1
    assert stored.loc[0, "symbol"] == "AAA"
    assert datasets["daily_bars"].symbols == 1


def test_import_recreates_missing_local_parquet_directory(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)
    cache.parquet_dir.rmdir()

    result = import_daily_bars_into_cache(
        cache=cache,
        warehouse=warehouse,
        frame=_bars(
            [
                ("AAA", "2024-01-02", 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, 1_500_000.0, False, False, 90),
            ]
        ),
        source="unit-test",
    )

    assert result.imported_rows == 1
    assert cache.daily_bars_path.exists() or cache.daily_bars_pickle_path.exists()
    assert len(cache.read_daily_bars()) == 1


def _coverage_days(start_date: str, end_date: str) -> list[pd.Timestamp]:
    return sorted(a_share_trade_dates(pd.Timestamp(start_date), pd.Timestamp(end_date)))


def _rows_for_days(symbol: str, days: list[pd.Timestamp], *, listing_days: int = 90) -> list[tuple[object, ...]]:
    return [
        (symbol, day, 10.0, 11.0, 9.0, 10.5, 1000, 0.1, 9_000_000_000.0, float("nan"), False, False, listing_days)
        for day in days
    ]


def test_fetch_daily_bars_marks_tail_coverage_shortfall_as_partial(tmp_path):
    cache = LocalCache(tmp_path)
    days = _coverage_days("2026-06-01", "2026-06-15")

    def fake_fetcher(symbols: list[str], start_date: str, end_date: str) -> pd.DataFrame:
        assert (start_date, end_date) == ("2026-06-01", "2026-06-15")
        return _bars(_rows_for_days("AAA", days[:3]))

    result = fetch_daily_bars_into_cache(
        cache=cache,
        fetcher=fake_fetcher,
        symbols=["AAA"],
        start_date="2026-06-01",
        end_date="2026-06-15",
    )

    shortfalls = [item for item in result.diagnostics if item.get("code") == "date_coverage_shortfall"]
    assert result.status == "partial"
    assert "AAA" in result.missing_symbols
    assert [item["symbol"] for item in shortfalls] == ["AAA"]
    assert shortfalls[0]["source"] == "daily_bars_fetcher"
    assert "2026-06-15" in str(shortfalls[0]["message"])
    assert any(entry.level == "warning" and "AAA" in entry.message for entry in result.logs)


def test_fetch_daily_bars_does_not_flag_delisted_symbol_tail_as_shortfall(tmp_path):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)
    warehouse.upsert_symbol_lifecycle(
        [{"symbol": "AAA", "listing_date": "2026-05-01", "delisted_date": "2026-06-05", "status": "delisted"}]
    )
    days = _coverage_days("2026-06-01", "2026-06-15")
    delisted_tail = [day for day in days if day <= pd.Timestamp("2026-06-05")]
    assert delisted_tail[-1] == pd.Timestamp("2026-06-05")

    def fake_fetcher(symbols: list[str], start_date: str, end_date: str) -> pd.DataFrame:
        return _bars(_rows_for_days("AAA", delisted_tail, listing_days=9999))

    result = fetch_daily_bars_into_cache(
        cache=cache,
        fetcher=fake_fetcher,
        symbols=["AAA"],
        start_date="2026-06-01",
        end_date="2026-06-15",
        warehouse=warehouse,
    )

    assert result.status == "ok"
    assert result.missing_symbols == []
    assert not any(item.get("code") == "date_coverage_shortfall" for item in result.diagnostics)


def test_fetch_daily_bars_full_window_return_reports_ok(tmp_path):
    cache = LocalCache(tmp_path)
    days = _coverage_days("2026-06-01", "2026-06-15")

    def fake_fetcher(symbols: list[str], start_date: str, end_date: str) -> pd.DataFrame:
        return _bars(_rows_for_days("AAA", days))

    result = fetch_daily_bars_into_cache(
        cache=cache,
        fetcher=fake_fetcher,
        symbols=["AAA"],
        start_date="2026-06-01",
        end_date="2026-06-15",
    )

    assert result.status == "ok"
    assert result.missing_symbols == []
    assert not any(item.get("code") == "date_coverage_shortfall" for item in result.diagnostics)


def test_coverage_drops_market_normal_day_gap_from_missing_trade_dates(tmp_path):
    warehouse = Warehouse(tmp_path)
    days = _coverage_days("2026-06-01", "2026-06-15")
    gap_day = days[4]
    rows = [
        *_rows_for_days("BBB", days),
        *_rows_for_days("CCC", days),
        *_rows_for_days("AAA", [day for day in days if day != gap_day]),
    ]
    warehouse.write_daily_bars(_bars(rows))

    details = build_daily_bars_coverage(
        cache=LocalCache(tmp_path),
        warehouse=warehouse,
        symbols=["AAA"],
        start_date="2026-06-01",
        end_date="2026-06-15",
    )

    missing = {day.isoformat() for day in details.items[0].missing_trade_dates}
    assert gap_day.date().isoformat() not in missing
    assert details.items[0].missing_trade_dates == []


def test_coverage_keeps_thin_day_gap_in_missing_trade_dates(tmp_path):
    warehouse = Warehouse(tmp_path)
    days = _coverage_days("2026-06-01", "2026-06-15")
    thin_day = days[4]
    rows = [
        *(row for row in _rows_for_days("AAA", days) if row[1] != thin_day),
        *(row for row in _rows_for_days("BBB", days) if row[1] != thin_day),
        *(row for row in _rows_for_days("CCC", days) if row[1] != thin_day),
    ]
    warehouse.write_daily_bars(_bars(rows))

    details = build_daily_bars_coverage(
        cache=LocalCache(tmp_path),
        warehouse=warehouse,
        symbols=["AAA"],
        start_date="2026-06-01",
        end_date="2026-06-15",
    )

    missing = {day.isoformat() for day in details.items[0].missing_trade_dates}
    assert missing == {thin_day.date().isoformat()}


def test_coverage_keeps_stale_tail_after_symbol_last_row(tmp_path):
    warehouse = Warehouse(tmp_path)
    days = _coverage_days("2026-06-01", "2026-06-15")
    rows = [
        *_rows_for_days("AAA", days[:5]),
        *_rows_for_days("BBB", days),
        *_rows_for_days("CCC", days),
    ]
    warehouse.write_daily_bars(_bars(rows))

    details = build_daily_bars_coverage(
        cache=LocalCache(tmp_path),
        warehouse=warehouse,
        symbols=["AAA"],
        start_date="2026-06-01",
        end_date="2026-06-15",
    )

    missing = {day.isoformat() for day in details.items[0].missing_trade_dates}
    assert missing == {day.date().isoformat() for day in days[5:]}


def test_coverage_keeps_flat_calendar_without_warehouse(tmp_path):
    cache = LocalCache(tmp_path)
    days = _coverage_days("2026-06-01", "2026-06-15")
    gap_day = days[4]
    cache.write_daily_bars(_bars(_rows_for_days("AAA", [day for day in days if day != gap_day])))

    details = build_daily_bars_coverage(
        cache=cache,
        warehouse=None,
        symbols=["AAA"],
        start_date="2026-06-01",
        end_date="2026-06-15",
    )

    missing = {day.isoformat() for day in details.items[0].missing_trade_dates}
    assert missing == {gap_day.date().isoformat()}


def test_coverage_keeps_flat_calendar_when_market_trade_date_counts_fail(tmp_path, monkeypatch):
    warehouse = Warehouse(tmp_path)
    days = _coverage_days("2026-06-01", "2026-06-15")
    gap_day = days[4]
    rows = [
        *_rows_for_days("BBB", days),
        *_rows_for_days("CCC", days),
        *_rows_for_days("AAA", [day for day in days if day != gap_day]),
    ]
    warehouse.write_daily_bars(_bars(rows))

    def boom(*args, **kwargs):
        raise RuntimeError("counts unavailable")

    monkeypatch.setattr(warehouse, "market_trade_date_counts", boom)

    details = build_daily_bars_coverage(
        cache=LocalCache(tmp_path),
        warehouse=warehouse,
        symbols=["AAA"],
        start_date="2026-06-01",
        end_date="2026-06-15",
    )

    missing = {day.isoformat() for day in details.items[0].missing_trade_dates}
    assert missing == {gap_day.date().isoformat()}


def test_coverage_keeps_missing_dates_when_window_is_below_classification_minimum(tmp_path):
    warehouse = Warehouse(tmp_path)
    days = _coverage_days("2026-06-01", "2026-06-03")
    gap_day = days[1]
    rows = [
        *_rows_for_days("BBB", days),
        *_rows_for_days("CCC", days),
        *_rows_for_days("AAA", [day for day in days if day != gap_day]),
    ]
    warehouse.write_daily_bars(_bars(rows))

    details = build_daily_bars_coverage(
        cache=LocalCache(tmp_path),
        warehouse=warehouse,
        symbols=["AAA"],
        start_date=days[0].date().isoformat(),
        end_date=days[-1].date().isoformat(),
    )

    missing = {day.isoformat() for day in details.items[0].missing_trade_dates}
    assert missing == {gap_day.date().isoformat()}


def test_coverage_keeps_thin_day_gap_when_calendar_has_zero_row_days(tmp_path):
    """0 行日不能混进横截面中位数。

    market_trade_date_counts 按交易日历给每个交易日补 0；把 0 混进中位数会把
    阈值压到 1，让“只有 1 行”的 06-03 被误判成市场正常日、缺口被吞掉。
    过滤 0 后中位数是 4、阈值 2，06-03 是 thin day 照算，0 行日也不在
    market_normal_days 里、按平日历口径保留——与 coverage()/sync 两个出口一致。
    """
    warehouse = Warehouse(tmp_path)
    days = _coverage_days("2026-06-01", "2026-06-09")
    assert [day.date().isoformat() for day in days] == [
        "2026-06-01",
        "2026-06-02",
        "2026-06-03",
        "2026-06-04",
        "2026-06-05",
        "2026-06-08",
        "2026-06-09",
    ]
    thin_day = days[2]  # 06-03：只有 DDD 有行
    full_tail = [days[0], days[1], days[6]]
    rows = [
        *_rows_for_days("AAA", full_tail),
        *_rows_for_days("BBB", full_tail),
        *_rows_for_days("CCC", full_tail),
        *_rows_for_days("DDD", [*full_tail, thin_day]),
    ]
    warehouse.write_daily_bars(_bars(rows))

    details = build_daily_bars_coverage(
        cache=LocalCache(tmp_path),
        warehouse=warehouse,
        symbols=["AAA"],
        start_date="2026-06-01",
        end_date="2026-06-09",
    )

    missing = sorted(day.isoformat() for day in details.items[0].missing_trade_dates)
    assert missing == ["2026-06-03", "2026-06-04", "2026-06-05", "2026-06-08"]


def _fail_first_cache_write(monkeypatch) -> list[int]:
    calls: list[int] = []
    original_write = LocalCache.write_daily_bars

    def flaky_write(self, frame):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise FileLockTimeout(str(self.daily_bars_path), 120.0)
        return original_write(self, frame)

    monkeypatch.setattr(LocalCache, "write_daily_bars", flaky_write)
    monkeypatch.setattr(operations, "WRITE_LOCK_RETRY_BACKOFF_SECONDS", 0)
    return calls


def test_fetch_daily_bars_retries_cache_write_after_lock_timeout(tmp_path, monkeypatch):
    cache = LocalCache(tmp_path)
    days = _coverage_days("2026-06-01", "2026-06-02")
    calls = _fail_first_cache_write(monkeypatch)

    result = fetch_daily_bars_into_cache(
        cache=cache,
        fetcher=lambda symbols, start_date, end_date: _bars(_rows_for_days("AAA", days)),
        symbols=["AAA"],
        start_date="2026-06-01",
        end_date="2026-06-02",
        refresh_coverage=False,
    )

    assert calls == [1, 2]
    assert result.status == "ok"
    assert len(cache.read_daily_bars()) == len(days)


def test_import_daily_bars_retries_cache_write_after_lock_timeout(tmp_path, monkeypatch):
    cache = LocalCache(tmp_path)
    days = _coverage_days("2026-06-01", "2026-06-02")
    calls = _fail_first_cache_write(monkeypatch)

    result = import_daily_bars_into_cache(
        cache=cache,
        frame=_bars(_rows_for_days("AAA", days)),
        source="unit-test",
    )

    assert calls == [1, 2]
    assert result.status == "ok"
    assert len(cache.read_daily_bars()) == len(days)


def test_fetch_capital_flow_retries_cache_write_after_lock_timeout(tmp_path, monkeypatch):
    cache = LocalCache(tmp_path)
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(_bars(_rows_for_days("AAA", [pd.Timestamp("2024-01-02")])))

    def fake_capital_flow_fetcher(symbols: list[str], start_date: str, end_date: str) -> dict:
        return {
            "rows": [{"symbol": "AAA", "trade_date": "2024-01-02", "main_net_inflow": 1_500_000.0}],
            "failures": [],
            "diagnostics": [],
        }

    calls = _fail_first_cache_write(monkeypatch)

    result = fetch_capital_flow_into_cache(
        cache=cache,
        warehouse=warehouse,
        capital_flow_fetcher=fake_capital_flow_fetcher,
        symbols=["AAA"],
        start_date="2024-01-02",
        end_date="2024-01-02",
        refresh_coverage=False,
    )

    assert calls == [1, 2]
    assert result.status == "ok"
    assert result.imported_rows == 1
    assert cache.read_daily_bars()["main_net_inflow"].tolist() == [1_500_000.0]


def test_aggregate_ths_hot_topics_ranks_normalized_topics():
    topics = _aggregate_ths_hot_topics(
        [
            {"code": "300001", "name": "A", "reason": "算力租赁+AI政务", "zhangfu": 9.9, "chengjiaoe": 100000},
            {"code": "300002", "name": "B", "reason": "算力租赁+液冷服务器", "zhangfu": 8.2, "chengjiaoe": 90000},
            {"code": "300003", "name": "C", "reason": "液冷服务器+AI政务", "zhangfu": 6.8, "chengjiaoe": 70000},
        ]
    )

    assert topics
    assert topics[0].name == "算力租赁"
    assert topics[0].source == "ths-hot-reason"
    assert all(topic.name not in {"", "A股", "市场"} for topic in topics)
