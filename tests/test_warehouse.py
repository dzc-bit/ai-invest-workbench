from __future__ import annotations

import pandas as pd
import pytest
from astock_backtester.data.trading_calendar import a_share_trade_dates
from astock_backtester.data.warehouse import Warehouse
from pyarrow.lib import ArrowInvalid


def _bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["600519", "600519", "000001"],
            "trade_date": ["2015-01-05", "2016-01-04", "2016-01-04"],
            "open": [10.0, 11.0, 8.0],
            "high": [10.5, 11.5, 8.5],
            "low": [9.8, 10.8, 7.9],
            "close": [10.2, 11.2, 8.1],
            "volume": [1000, 1200, 900],
            "amount": [10200.0, 13440.0, 7290.0],
            "float_market_cap": [1000000000.0, 1100000000.0, 800000000.0],
            "total_market_cap": [1200000000.0, 1300000000.0, 900000000.0],
        }
    )


def test_warehouse_writes_year_partitions_and_reads_filtered_data(tmp_path):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(_bars())

    assert (tmp_path / "warehouse" / "daily_bars" / "year=2015" / "daily_bars.parquet").exists()
    assert (tmp_path / "warehouse" / "daily_bars" / "year=2016" / "daily_bars.parquet").exists()

    result = warehouse.read_daily_bars(
        symbols=["600519"],
        start_date="2016-01-01",
        end_date="2016-12-31",
    )

    assert result["symbol"].tolist() == ["600519"]
    assert result["trade_date"].dt.strftime("%Y-%m-%d").tolist() == ["2016-01-04"]


def test_warehouse_reads_only_year_partitions_overlapping_requested_dates(tmp_path, monkeypatch):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(_bars())

    read_paths = []
    original_read_parquet = pd.read_parquet

    def tracking_read_parquet(path, *args, **kwargs):
        read_paths.append(str(path))
        return original_read_parquet(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", tracking_read_parquet)

    result = warehouse.read_daily_bars(start_date="2016-01-01", end_date="2016-12-31")

    assert set(result["symbol"]) == {"000001", "600519"}
    assert all("year=2016" in path for path in read_paths)
    assert read_paths


def test_warehouse_safe_read_parquet_only_treats_missing_files_as_empty(tmp_path, monkeypatch):
    warehouse = Warehouse(tmp_path)

    assert warehouse._safe_read_parquet(tmp_path / "missing.parquet").empty

    def broken_read_parquet(*_args, **_kwargs):
        raise OSError("corrupt parquet")

    monkeypatch.setattr(pd, "read_parquet", broken_read_parquet)

    with pytest.raises(OSError, match="corrupt parquet"):
        warehouse._safe_read_parquet(tmp_path / "corrupt.parquet")


def test_warehouse_reads_daily_symbols_without_dropping_symbols_missing_recent_dates(tmp_path):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001", "000002", "000003"],
                "trade_date": ["2026-06-18", "2026-06-17", "2026-06-18"],
                "open": [1.0, 2.0, float("nan")],
                "high": [1.0, 2.0, float("nan")],
                "low": [1.0, 2.0, float("nan")],
                "close": [1.0, 2.0, float("nan")],
                "volume": [1, 1, 1],
            }
        )
    )

    all_symbols = warehouse.read_daily_symbols(require_ohlc=True)
    latest_symbols = warehouse.read_daily_symbols(
        start_date="2026-06-18",
        end_date="2026-06-18",
        require_ohlc=True,
    )

    assert all_symbols == ["000001", "000002"]
    assert latest_symbols == ["000001"]


def test_warehouse_merges_rows_by_symbol_and_trade_date(tmp_path):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(_bars())
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["600519"],
                "trade_date": ["2016-01-04"],
                "open": [12.0],
                "high": [12.5],
                "low": [11.8],
                "close": [12.2],
                "volume": [2200],
                "amount": [26840.0],
                "float_market_cap": [1500000000.0],
                "total_market_cap": [1600000000.0],
            }
        )
    )

    result = warehouse.read_daily_bars(symbols=["600519"], start_date="2016-01-04", end_date="2016-01-04")

    assert len(result) == 1
    assert result.loc[0, "close"] == 12.2
    assert result.loc[0, "float_market_cap"] == 1500000000.0


def test_warehouse_coverage_reports_daily_and_market_cap(tmp_path):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(_bars())

    coverage = {item.dataset: item for item in warehouse.coverage()}

    assert coverage["daily_bars"].symbols == 2
    assert coverage["daily_bars"].start_date.isoformat() == "2015-01-05"
    assert coverage["market_cap"].missing_rows == 0


def test_warehouse_coverage_uses_partition_stats_without_full_read(tmp_path, monkeypatch):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(_bars())

    def fail_full_read(*args, **kwargs):
        raise AssertionError("coverage should not load the full warehouse")

    monkeypatch.setattr(warehouse, "read_daily_bars", fail_full_read)

    coverage = {item.dataset: item for item in warehouse.coverage()}

    assert coverage["daily_bars"].symbols == 2
    assert coverage["daily_bars"].start_date.isoformat() == "2015-01-05"
    assert coverage["daily_bars"].end_date.isoformat() == "2016-01-04"
    # 口径修订后：600519 窗口内的内部洞落在“无任何行”的交易日上——横截面
    # 证据下这些日子的缺行方向是可补（thin），记入 missing_rows；两只股票都
    # 同步到窗口末行 → 尾部缺口 0 → suspension_rows 为 0。
    span = len(a_share_trade_dates(pd.Timestamp("2015-01-05"), pd.Timestamp("2016-01-04")))
    assert coverage["daily_bars"].missing_rows == span - 2
    assert coverage["daily_bars"].suspension_rows == 0
    assert coverage["market_cap"].symbols == 2
    assert coverage["market_cap"].missing_rows == 0
    assert coverage["capital_flow"].symbols == 0
    assert coverage["capital_flow"].missing_rows == 3


def test_warehouse_coverage_counts_latest_daily_rows_missing_from_known_symbols(tmp_path):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001", "000002", "000003", "000001"],
                "trade_date": ["2026-06-05", "2026-06-05", "2026-06-05", "2026-06-08"],
                "open": [10.0, 10.0, 10.0, 10.2],
                "high": [10.5, 10.5, 10.5, 10.4],
                "low": [9.8, 9.8, 9.8, 10.0],
                "close": [10.2, 10.2, 10.2, 10.3],
                "volume": [1000, 1000, 1000, 1200],
            }
        )
    )

    coverage = {item.dataset: item for item in warehouse.coverage()}

    assert coverage["daily_bars"].symbols == 3
    assert coverage["daily_bars"].end_date.isoformat() == "2026-06-08"
    assert coverage["daily_bars"].missing_rows == 2


def test_warehouse_separates_capital_flow_only_rows_from_daily_bar_coverage(tmp_path):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001", "000002"],
                "trade_date": ["2026-06-05", "2026-06-05"],
                "open": [10.0, float("nan")],
                "high": [10.5, float("nan")],
                "low": [9.8, float("nan")],
                "close": [10.2, float("nan")],
                "volume": [1000, 0],
                "main_net_inflow": [float("nan"), 1_000_000.0],
            }
        )
    )

    coverage = {item.dataset: item for item in warehouse.coverage()}
    tradable = warehouse.read_daily_bars(require_ohlc=True)
    all_rows = warehouse.read_daily_bars()

    assert coverage["daily_bars"].symbols == 1
    assert coverage["daily_bars"].missing_rows == 0
    assert coverage["capital_flow"].symbols == 1
    assert coverage["capital_flow"].missing_rows == 1
    assert tradable["symbol"].tolist() == ["000001"]
    assert all_rows["symbol"].tolist() == ["000001", "000002"]


def test_warehouse_coverage_ignores_known_public_capital_flow_source_gaps(tmp_path):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001", "000001"],
                "trade_date": ["2019-04-04", "2019-04-08"],
                "open": [10.0, 10.0],
                "high": [10.5, 10.5],
                "low": [9.8, 9.8],
                "close": [10.2, 10.2],
                "volume": [1000, 1000],
                "main_net_inflow": [float("nan"), float("nan")],
            }
        )
    )

    coverage = {item.dataset: item for item in warehouse.coverage()}

    assert coverage["capital_flow"].missing_rows == 1


def test_warehouse_coverage_ignores_rows_before_symbol_capital_flow_source_start(tmp_path):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["920001", "920001", "920001"],
                "trade_date": ["2021-11-12", "2021-11-15", "2021-11-16"],
                "open": [10.0, 10.0, 10.0],
                "high": [10.5, 10.5, 10.5],
                "low": [9.8, 9.8, 9.8],
                "close": [10.2, 10.2, 10.2],
                "volume": [1000, 1000, 1000],
                "main_net_inflow": [float("nan"), 1_000_000.0, float("nan")],
            }
        )
    )

    coverage = {item.dataset: item for item in warehouse.coverage()}

    assert coverage["capital_flow"].missing_rows == 1


def test_warehouse_coverage_ignores_late_2021_public_source_start_gap(tmp_path):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["688272", "688272", "688272"],
                "trade_date": ["2021-12-28", "2021-12-29", "2021-12-30"],
                "open": [10.0, 10.0, 10.0],
                "high": [10.5, 10.5, 10.5],
                "low": [9.8, 9.8, 9.8],
                "close": [10.2, 10.2, 10.2],
                "volume": [1000, 1000, 1000],
                "main_net_inflow": [float("nan"), 1_000_000.0, float("nan")],
            }
        )
    )

    coverage = {item.dataset: item for item in warehouse.coverage()}

    assert coverage["capital_flow"].missing_rows == 1


def test_warehouse_coverage_uses_global_capital_flow_start_across_partitions(tmp_path):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["688272", "688272", "688272"],
                "trade_date": ["2020-12-31", "2021-12-28", "2021-12-29"],
                "open": [10.0, 10.0, 10.0],
                "high": [10.5, 10.5, 10.5],
                "low": [9.8, 9.8, 9.8],
                "close": [10.2, 10.2, 10.2],
                "volume": [1000, 1000, 1000],
                "main_net_inflow": [float("nan"), float("nan"), 1_000_000.0],
            }
        )
    )

    coverage = {item.dataset: item for item in warehouse.coverage()}

    assert coverage["capital_flow"].missing_rows == 0


def test_warehouse_coverage_ignores_short_listing_lag_but_counts_later_capital_flow_gaps(tmp_path):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                # 000001 追两行到全局最新日期，避免被“停更尾部缺口”口径计入缺失。
                "symbol": ["603027", "603027", "603027", "000001", "000001", "000001", "000001"],
                "trade_date": [
                    "2016-03-07",
                    "2016-03-08",
                    "2016-03-09",
                    "2015-01-05",
                    "2015-01-06",
                    "2016-03-08",
                    "2016-03-09",
                ],
                "open": [10.0, 10.0, 10.0, 9.0, 9.1, 9.2, 9.3],
                "high": [10.5, 10.5, 10.5, 9.5, 9.6, 9.7, 9.8],
                "low": [9.8, 9.8, 9.8, 8.8, 8.9, 9.0, 9.1],
                "close": [10.2, 10.2, 10.2, 9.2, 9.3, 9.4, 9.5],
                "volume": [1000, 1000, 1000, 900, 900, 900, 900],
                "main_net_inflow": [
                    float("nan"),
                    1_000_000.0,
                    float("nan"),
                    float("nan"),
                    500_000.0,
                    300_000.0,
                    200_000.0,
                ],
            }
        )
    )

    coverage = {item.dataset: item for item in warehouse.coverage()}

    assert coverage["capital_flow"].missing_rows == 2


def test_warehouse_reads_latest_daily_bars_from_recent_partitions(tmp_path):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(_bars())

    latest = warehouse.read_latest_daily_bars(days=1)

    assert latest["trade_date"].dt.strftime("%Y-%m-%d").tolist() == ["2016-01-04", "2016-01-04"]
    assert set(latest["symbol"]) == {"000001", "600519"}


def test_warehouse_latest_daily_bars_ignore_capital_flow_only_rows(tmp_path):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001", "000002"],
                "trade_date": ["2026-06-05", "2026-06-05"],
                "open": [10.0, float("nan")],
                "high": [10.5, float("nan")],
                "low": [9.8, float("nan")],
                "close": [10.2, float("nan")],
                "volume": [1000, 0],
                "main_net_inflow": [float("nan"), 1_000_000.0],
            }
        )
    )

    latest = warehouse.read_latest_daily_bars(days=1)

    assert latest["symbol"].tolist() == ["000001"]
    assert latest[["open", "high", "low", "close"]].notna().all().all()


def test_warehouse_surfaces_corrupt_recent_partition_for_latest_and_coverage(tmp_path):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(_bars())
    corrupt_path = tmp_path / "warehouse" / "daily_bars" / "year=2026" / "daily_bars.parquet"
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_bytes(b"not a parquet file")

    with pytest.raises(ArrowInvalid, match="Parquet magic bytes"):
        warehouse.read_latest_daily_bars(days=1)
    with pytest.raises(ArrowInvalid, match="Parquet magic bytes"):
        warehouse.read_daily_symbols(require_ohlc=True)
    with pytest.raises(ArrowInvalid, match="Parquet magic bytes"):
        warehouse.coverage()


def test_warehouse_does_not_overwrite_corrupt_partition_when_new_rows_arrive(tmp_path):
    warehouse = Warehouse(tmp_path)
    corrupt_path = tmp_path / "warehouse" / "daily_bars" / "year=2026" / "daily_bars.parquet"
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_bytes(b"not a parquet file")

    with pytest.raises(ArrowInvalid, match="Parquet magic bytes"):
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
                }
            )
        )

    assert corrupt_path.read_bytes() == b"not a parquet file"


def test_warehouse_symbol_lifecycle_roundtrip_and_delisted_lookup(tmp_path):
    warehouse = Warehouse(tmp_path)

    assert warehouse.read_symbol_lifecycle() == {}
    assert warehouse.read_delisted_symbols() == set()

    written = warehouse.upsert_symbol_lifecycle(
        [
            {"symbol": "600519", "listing_date": "2001-08-27", "status": "listed"},
            {"symbol": "000002", "listing_date": "1991-01-29", "delisted_date": "2026-05-30", "status": "delisted"},
            {"symbol": "600001", "listing_date": "2026-06-01", "status": "listed"},
        ]
    )
    assert written == 3

    lifecycle = warehouse.read_symbol_lifecycle()
    assert lifecycle["600519"] == {"listing_date": "2001-08-27", "delisted_date": None, "status": "listed"}
    assert lifecycle["000002"]["delisted_date"] == "2026-05-30"
    # A later upsert without a listing date keeps the existing one via COALESCE.
    warehouse.upsert_symbol_lifecycle([{"symbol": "600001", "delisted_date": "2026-06-10", "status": "delisted"}])
    lifecycle = warehouse.read_symbol_lifecycle(symbols=["600001"])
    assert lifecycle["600001"]["listing_date"] == "2026-06-01"
    assert lifecycle["600001"]["delisted_date"] == "2026-06-10"

    assert warehouse.read_delisted_symbols() == {"000002", "600001"}


def test_warehouse_capital_flow_missing_symbols_ignores_rows_outside_lifecycle_window(tmp_path):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["600001", "600001", "600002", "600003"],
                "trade_date": ["2026-06-01", "2026-06-02", "2026-06-02", "2026-06-02"],
                "open": [10.0, 10.0, 20.0, 30.0],
                "high": [10.5, 10.5, 20.5, 30.5],
                "low": [9.8, 9.8, 19.8, 29.8],
                "close": [10.2, 10.2, 20.2, 30.2],
                "volume": [1000, 1000, 2000, 3000],
                "main_net_inflow": [100.0, float("nan"), float("nan"), float("nan")],
            }
        )
    )
    warehouse.upsert_symbol_lifecycle(
        [
            # 600001 traded on 06-01 (with flow) and shows a stray 06-02 row
            # after its delisting date; the stray row must not mark it missing.
            {"symbol": "600001", "delisted_date": "2026-06-01", "status": "delisted"},
            # 600002 has no lifecycle record: legacy conservative behaviour.
            # 600003 was delisted before the window entirely.
            {"symbol": "600003", "delisted_date": "2026-05-30", "status": "delisted"},
        ]
    )

    missing = warehouse.read_capital_flow_missing_symbols("2026-06-01", "2026-06-02")

    assert missing == {"600002"}


def test_warehouse_coverage_daily_missing_rows_excludes_delisted_symbols(tmp_path):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001", "000002", "000003", "000001"],
                "trade_date": ["2026-06-05", "2026-06-05", "2026-06-05", "2026-06-08"],
                "open": [10.0, 10.0, 10.0, 10.2],
                "high": [10.5, 10.5, 10.5, 10.4],
                "low": [9.8, 9.8, 9.8, 10.0],
                "close": [10.2, 10.2, 10.2, 10.3],
                "volume": [1000, 1000, 1000, 1200],
            }
        )
    )
    warehouse.upsert_symbol_lifecycle(
        [{"symbol": "000002", "delisted_date": "2026-06-05", "status": "delisted"}]
    )

    coverage = {item.dataset: item for item in warehouse.coverage()}

    assert coverage["daily_bars"].missing_rows == 1


def test_data_gap_profile_reports_stale_tails_and_thin_days(tmp_path):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                # A 更新到 06-04；B/C 停在 06-03 → 停更尾部 {06-03: 2}
                "symbol": ["000001"] * 4 + ["000002"] * 3 + ["000003"] * 3,
                "trade_date": [
                    "2026-06-01",
                    "2026-06-02",
                    "2026-06-03",
                    "2026-06-04",
                    "2026-06-01",
                    "2026-06-02",
                    "2026-06-03",
                    "2026-06-01",
                    "2026-06-02",
                    "2026-06-03",
                ],
                "open": [10.0] * 10,
                "high": [10.5] * 10,
                "low": [9.8] * 10,
                "close": [10.2] * 10,
                "volume": [1000] * 10,
                # 资金流：B/C 只在 06-01 有 → 资金流停更尾部 {06-01: 2}
                "main_net_inflow": [
                    100.0,
                    100.0,
                    100.0,
                    100.0,
                    50.0,
                    float("nan"),
                    float("nan"),
                    50.0,
                    float("nan"),
                    float("nan"),
                ],
            }
        )
    )

    profile = warehouse.data_gap_profile()

    assert profile["available"] is True
    window = profile["window"]
    assert window["end_date"] == "2026-06-04"
    daily = profile["daily_bars"]
    assert daily["symbols"] == 3
    assert daily["symbols_current"] == 1
    assert daily["symbols_stale"] == 2
    assert {"last_date": "2026-06-03", "symbols": 2} in daily["stale_distribution"]
    # 06-04 只有 1 行（其余交易日 3 行）→ 疑似写入失败日
    assert {"trade_date": "2026-06-04", "rows": 1} in daily["thin_days"]
    # 资金流 B/C 停在 06-01
    assert {"last_date": "2026-06-01", "symbols": 2} in profile["capital_flow"]["stale_distribution"]


def test_data_gap_profile_caches_until_invalidated(tmp_path):
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(_bars())
    first = warehouse.data_gap_profile()
    assert first["available"] is True
    # 未写入时命中缓存（10 分钟 TTL）
    assert warehouse.data_gap_profile() is first

    warehouse.write_daily_bars(_bars().assign(close=[10.3, 11.3, 8.1]))

    # 写入后自动失效，重算出新的画像
    second = warehouse.data_gap_profile()
    assert second is not first


def test_data_gap_profile_empty_warehouse_reports_unavailable(tmp_path):
    warehouse = Warehouse(tmp_path)
    profile = warehouse.data_gap_profile()
    assert profile["available"] is False


def test_build_daily_bars_coverage_sees_tail_gap_without_explicit_end_date(tmp_path):
    """Summary coverage and per-symbol coverage must agree on tail gaps.

    ``Warehouse.coverage()`` counts the days after a symbol's last row; the
    per-symbol endpoint used to end its window at that symbol's own last row,
    so a stalled stock reported ``missing=0`` there while the summary card
    reported a huge backlog — the "coverage says fine, sync says top-up"
    contradiction.
    """
    from astock_backtester.data.cache import LocalCache
    from astock_backtester.data.operations import build_daily_bars_coverage

    warehouse = Warehouse(tmp_path)
    # 600519 一直写到 2016-01-04；000001 停在 2015-01-05（尾部缺口）。
    frame = pd.DataFrame(
        {
            "symbol": ["000001", "600519", "600519"],
            "trade_date": ["2015-01-05", "2015-01-05", "2016-01-04"],
            "open": [8.0, 10.0, 11.0],
            "high": [8.5, 10.5, 11.5],
            "low": [7.9, 9.8, 10.8],
            "close": [8.1, 10.2, 11.2],
            "volume": [900, 1000, 1200],
            "amount": [7290.0, 10200.0, 13440.0],
            "float_market_cap": [800000000.0, 1000000000.0, 1100000000.0],
        }
    )
    warehouse.write_daily_bars(frame)
    cache = LocalCache(tmp_path)

    without_end = build_daily_bars_coverage(cache, warehouse, symbols=["000001"])
    item = without_end.items[0]
    # 2015-01-05 之后到仓库最新日（2016-01-04）之间的交易日都应算缺口。
    assert item.end_date.isoformat() == "2015-01-05"
    assert len(item.missing_trade_dates) > 0

    explicit_end = build_daily_bars_coverage(cache, warehouse, symbols=["000001"], end_date="2016-01-04")
    assert len(explicit_end.items[0].missing_trade_dates) == len(item.missing_trade_dates)


def test_coverage_classifies_internal_gaps_by_cross_section_evidence(tmp_path):
    """内部洞按横截面证据分类：市场正常日=停牌（不可补），thin day=疑似写入失败（可补）。

    停更尾部不参与分类，永远记入 missing_rows——它是“多久没同步”的可行动信号。
    """
    warehouse = Warehouse(tmp_path)
    layout = {
        # 000001 全程在场；000002 只在 06-01（06-02/03/04 全是停更尾部）；
        # 000003 缺 06-02（thin day）与 06-03（市场正常日→停牌类）；
        # 000004/5/6 只缺 06-02（thin day→可补）。
        "000001": ("2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"),
        "000002": ("2026-06-01",),
        "000003": ("2026-06-01", "2026-06-04"),
        "000004": ("2026-06-01", "2026-06-03", "2026-06-04"),
        "000005": ("2026-06-01", "2026-06-03", "2026-06-04"),
        "000006": ("2026-06-01", "2026-06-03", "2026-06-04"),
    }
    rows = [
        {
            "symbol": symbol,
            "trade_date": day,
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "volume": 1000,
        }
        for symbol, days in layout.items()
        for day in days
    ]
    warehouse.write_daily_bars(pd.DataFrame(rows))

    coverage = {item.dataset: item for item in warehouse.coverage()}
    daily = coverage["daily_bars"]

    # 行数分布：06-01=6、06-02=1、06-03=4、06-04=6 → 中位数 5，thin 阈值 2.5：
    # - 06-02 thin day：spanning 6 − 实有 1 = 5（000003..000006，000002 不跨此日）→ missing_rows
    # - 06-03 市场正常：spanning 5 − 实有 4 = 1（000003）→ suspension_rows
    # - 06-04 市场正常：spanning 6 − 实有 6 = 0
    # - 尾部：000002 止于 06-01 → 3 个交易日 → missing_rows
    assert daily.missing_rows == 7
    assert daily.suspension_rows == 1


def test_coverage_capital_flow_counts_symbols_absent_from_flow(tmp_path):
    """资金流尾部缺口必须覆盖“日线有、资金流完全没有”的股票。

    旧口径只遍历 ``flow_rows_by_symbol``，从未采到资金流的股票不在其中，
    与 ``market_cap`` 口径不一致：它们不产生任何尾部缺口计数。

    用例设计：600519 资金流字段全空且停在 06-01（两行内部缺口 + 06-02/03
    两个交易日尾部缺口），000001 资金流完整。修复前 capital_flow 的尾部
    贡献为 0；修复后它必须严格大于内部缺口（即 2）。
    """
    warehouse = Warehouse(tmp_path)
    frame = pd.DataFrame(
        {
            "symbol": ["000001", "000001", "000001", "600519", "600519"],
            "trade_date": ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-01", "2026-06-02"],
            "open": [8.0, 8.1, 8.2, 10.0, 10.2],
            "high": [8.5, 8.6, 8.6, 10.5, 10.6],
            "low": [7.9, 8.0, 8.1, 9.8, 10.1],
            "close": [8.1, 8.2, 8.3, 10.2, 10.4],
            "volume": [900, 910, 920, 1000, 1100],
            "amount": [7290.0, 7462.0, 7636.0, 10200.0, 11440.0],
            "float_market_cap": [8e8, 8.05e8, 8.1e8, 1e9, 1.02e9],
            # 000001 资金流完整；600519 资金流全空 → 只有它是“从未采到资金流”的股票。
            "main_net_inflow": [1200.0, 1210.0, 1220.0, None, None],
        }
    )
    warehouse.write_daily_bars(frame)

    coverage = {item.dataset: item for item in warehouse.coverage()}

    # 内部缺口恰好 2 行（600519 的两行空字段）。
    # 修复前尾部缺口为 0 → missing == 2；修复后必须把 600519 尾部交易日计入。
    assert coverage["capital_flow"].missing_rows > 2
    # 日线最新日 06-03，600519 数据停在 06-02 → 至少 1 个尾部交易日。
    assert coverage["capital_flow"].missing_rows >= 3


def test_coverage_capital_flow_tail_respects_standalone_flow_rows(tmp_path):
    """独立资金流行把资金流末行推到日线末行之后时，覆盖的日期不算缺口。

    §8 允许“暂无日 K 也先写资金流独立行”。旧口径把资金流尾部边界强制取
    日线末行，独立行覆盖的每个交易日被反复计成缺口——表现为补齐资金流后
    coverage 的缺口不降，而缺口画像（按 flow 末行）显示已最新，两个视图矛盾。

    000001 是参照股：OHLC 更新到全局最新日 06-04，把 `daily_end` 推到 06-04。
    没有它时窗口终点就是 600519 自己的日线末行 06-02，旧口径同样返回 0，
    测试锁不住边界修复。
    """
    warehouse = Warehouse(tmp_path)
    # 600519：OHLC 只到 06-02；06-03/06-04 是资金流独立行（OHLC 全空）。
    # 000001：OHLC 与资金流都完整到 06-04。
    frame = pd.DataFrame(
        {
            "symbol": ["600519", "600519", "000001", "000001", "000001", "000001"],
            "trade_date": ["2026-06-01", "2026-06-02", "2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"],
            "open": [10.0, 10.1, 8.0, 8.1, 8.2, 8.3],
            "high": [10.5, 10.6, 8.5, 8.6, 8.7, 8.8],
            "low": [9.8, 9.9, 7.9, 8.0, 8.1, 8.2],
            "close": [10.2, 10.3, 8.1, 8.2, 8.3, 8.4],
            "volume": [1000, 1001, 900, 910, 920, 930],
            "amount": [10200.0, 10303.0, 7290.0, 7462.0, 7636.0, 7812.0],
            "float_market_cap": [1e9, 1.01e9, 8e8, 8.05e8, 8.1e8, 8.15e8],
            "main_net_inflow": [100.0, 100.0, 50.0, 51.0, 52.0, 53.0],
        }
    )
    standalone = pd.DataFrame(
        {
            "symbol": ["600519", "600519"],
            "trade_date": ["2026-06-03", "2026-06-04"],
            "open": [float("nan")] * 2,
            "high": [float("nan")] * 2,
            "low": [float("nan")] * 2,
            "close": [float("nan")] * 2,
            "volume": [0.0] * 2,
            "amount": [0.0] * 2,
            "main_net_inflow": [100.0, 100.0],
        }
    )
    warehouse.write_daily_bars(frame)
    warehouse.write_daily_bars(standalone)

    coverage = {item.dataset: item for item in warehouse.coverage()}

    # 旧口径：600519 的资金流边界取日线末行 06-02 → 06-03/06-04 被计成 2 个缺口。
    # 新口径：边界取 max(日线末行 06-02, 资金流末行 06-04) = 06-04 → 缺口为 0。
    assert coverage["capital_flow"].missing_rows == 0
    # 日线口径照算尾部：600519 的 OHLC 停在 06-02，全局最新日 06-04 → 缺 2 行。
    assert coverage["daily_bars"].missing_rows == 2


def test_read_capital_flow_missing_symbols_spans_window_partitions(tmp_path):
    """``read_capital_flow_missing_symbols`` 必须扫窗口覆盖的所有年分区。

    旧实现只读最新分区：横跨旧分区的补数窗口里，"资金流只存在于旧分区"
    的股票会被误判为缺流。窗口 2025-12-30 ~ 2026-01-05 的应有交易日是
    12-30、12-31、01-05（01-01~01-03 元旦休市）：
    - 000001 三天都有流（12-30/12-31 在 year=2025、01-05 在 year=2026），
      合并两个分区才判得出完整——只扫最新分区会把它误判成缺流；
    - 000002 旧分区有流、01-05 缺流 → "有洞"，必须选中（全空口径选不中）；
    - 000003 窗口内始终无资金流 → 选中。
    """
    warehouse = Warehouse(tmp_path)
    frame = pd.DataFrame(
        {
            "symbol": ["000001", "000001", "000001", "000002", "000002", "000003"],
            "trade_date": [
                "2025-12-30",
                "2025-12-31",
                "2026-01-05",
                "2025-12-30",
                "2026-01-05",
                "2026-01-05",
            ],
            "open": [10.0] * 6,
            "high": [10.5] * 6,
            "low": [9.8] * 6,
            "close": [10.2] * 6,
            "volume": [1000] * 6,
            "main_net_inflow": [50.0, 51.0, 52.0, 60.0, float("nan"), float("nan")],
        }
    )
    warehouse.write_daily_bars(frame)

    missing = warehouse.read_capital_flow_missing_symbols("2025-12-30", "2026-01-05")

    assert "000001" not in missing  # 两个分区合并后窗口内完整
    assert "000002" in missing  # 12-31 与 01-05 缺流：全空口径选不中
    assert "000003" in missing  # 窗口内始终无资金流


def test_data_gap_profile_excludes_delisted_from_stale_distribution(tmp_path):
    """退市股的停更是终态不是缺口：必须从停更分布剔除并单列 delisted_symbols。

    不剔除的话，UI 与 AI 会把退市股当成“需要补数据”，而实际上补齐链路
    永远无法让退市股“追上”最新交易日。
    """
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                # A 更新到 06-04；B/C 停在 06-03，其中 B 已退市。
                "symbol": ["000001"] * 4 + ["000002"] * 3 + ["000003"] * 3,
                "trade_date": [
                    "2026-06-01",
                    "2026-06-02",
                    "2026-06-03",
                    "2026-06-04",
                    "2026-06-01",
                    "2026-06-02",
                    "2026-06-03",
                    "2026-06-01",
                    "2026-06-02",
                    "2026-06-03",
                ],
                "open": [10.0] * 10,
                "high": [10.5] * 10,
                "low": [9.8] * 10,
                "close": [10.2] * 10,
                "volume": [1000] * 10,
            }
        )
    )
    warehouse.upsert_symbol_lifecycle([{"symbol": "000002", "delisted_date": "2026-06-03", "status": "delisted"}])

    profile = warehouse.data_gap_profile()

    daily = profile["daily_bars"]
    assert daily["symbols"] == 3
    assert daily["delisted_symbols"] == 1
    assert daily["symbols_stale"] == 1
    # 分布里只剩未退市的 000003
    assert {"last_date": "2026-06-03", "symbols": 1} in daily["stale_distribution"]


def test_read_capital_flow_missing_symbols_selects_partial_flow_holes(tmp_path):
    """口径从"全空"扩到"有洞"：窗口内缺任一应有交易日的资金流就选中。

    旧实现只返回窗口内一行 non-null ``main_net_inflow`` 都没有的股票，
    000002 这种"大部分日期有流、个别日期缺流"的股票永远进不了补齐名单。
    """
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001", "000001", "000002", "000002", "000003", "000003"],
                "trade_date": ["2026-06-01", "2026-06-02"] * 3,
                "open": [10.0] * 6,
                "high": [10.5] * 6,
                "low": [9.8] * 6,
                "close": [10.2] * 6,
                "volume": [1000] * 6,
                "main_net_inflow": [100.0, 200.0, 100.0, float("nan"), float("nan"), float("nan")],
            }
        )
    )

    missing = warehouse.read_capital_flow_missing_symbols("2026-06-01", "2026-06-02")

    # 000001 两天都有流；000002 只缺 06-02（有洞）；000003 全空。
    assert missing == {"000002", "000003"}


def test_read_capital_flow_missing_symbols_ignores_known_source_gap_dates(tmp_path):
    """``KNOWN_CAPITAL_FLOW_SOURCE_GAP_DATES`` 里的整日缺口不算缺失。

    2024-07-16 是已知公开资金流源缺口日：窗口 07-15~07-16 的应有交易日只剩
    07-15。000002 只有 07-16 的流，救不了它缺的 07-15；000001 有 07-15 的流
    → 完整（07-16 的空值不是洞）。
    """
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001", "000001", "000002", "000002"],
                "trade_date": ["2024-07-15", "2024-07-16", "2024-07-15", "2024-07-16"],
                "open": [10.0] * 4,
                "high": [10.5] * 4,
                "low": [9.8] * 4,
                "close": [10.2] * 4,
                "volume": [1000] * 4,
                "main_net_inflow": [100.0, float("nan"), float("nan"), 80.0],
            }
        )
    )

    missing = warehouse.read_capital_flow_missing_symbols("2024-07-15", "2024-07-16")

    assert missing == {"000002"}


def test_read_capital_flow_missing_symbols_skips_lifecycle_clipped_empty_window(tmp_path):
    """生命周期截断后没有任何应有交易日 → 不算缺失。

    600001 上市日（2026-06-05）晚于窗口末尾：窗口内的应有交易日对它全部
    落在上市前，截断后为空，两行空资金流不能把它算成缺口。
    """
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["600001", "600001"],
                "trade_date": ["2026-06-01", "2026-06-02"],
                "open": [10.0, 10.0],
                "high": [10.5, 10.5],
                "low": [9.8, 9.8],
                "close": [10.2, 10.2],
                "volume": [1000, 1000],
                "main_net_inflow": [float("nan"), float("nan")],
            }
        )
    )
    warehouse.upsert_symbol_lifecycle([{"symbol": "600001", "listing_date": "2026-06-05", "status": "listed"}])

    missing = warehouse.read_capital_flow_missing_symbols("2026-06-01", "2026-06-02")

    assert missing == set()


def test_read_capital_flow_missing_symbols_exempts_capital_flow_source_start(tmp_path):
    """资金流源起点滞后豁免：新股在数据源起点之前的日期不算缺口（与 coverage 同规则）。

    窗口 2026-06-01 ~ 2026-06-12（10 个交易日，分类样本充足）：
    - 000007 于 06-04 上市（lifecycle listing_date），首根日线 06-04、资金流从
      06-08 才开始（lag 4 ≤ 90）→ 起点前的 06-04/06-05 豁免，不进名单；
    - 600008 首根日线就是窗口起点（``first_daily <= warehouse_start`` 让
      variant 1 不成立），但首行 ``listing_days=0`` → 走 listing-day 变体，
      06-01~06-05 豁免，不进名单；
    - 000009 是老股（``listing_days=9999``）且资金流 06-09 才有 → 两个变体
      都不适用，06-01~06-08 照算缺口。
    """
    warehouse = Warehouse(tmp_path)
    all_days = [
        "2026-06-01",
        "2026-06-02",
        "2026-06-03",
        "2026-06-04",
        "2026-06-05",
        "2026-06-08",
        "2026-06-09",
        "2026-06-10",
        "2026-06-11",
        "2026-06-12",
    ]
    listings = {"000007": "2026-06-04", "600008": "2026-06-01", "000009": None}
    symbol_days = {"000007": all_days[3:], "600008": all_days, "000009": all_days}
    flow_start = {"000007": "2026-06-08", "600008": "2026-06-08", "000009": "2026-06-09"}
    rows = []
    for symbol in ("000007", "600008", "000009"):
        for day in symbol_days[symbol]:
            listing = listings[symbol]
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": day,
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "volume": 1000,
                    "main_net_inflow": 100.0 if day >= flow_start[symbol] else float("nan"),
                    "listing_days": 9999 if listing is None else (pd.Timestamp(day) - pd.Timestamp(listing)).days,
                }
            )
    warehouse.write_daily_bars(pd.DataFrame(rows))
    warehouse.upsert_symbol_lifecycle([{"symbol": "000007", "listing_date": "2026-06-04", "status": "listed"}])

    missing = warehouse.read_capital_flow_missing_symbols("2026-06-01", "2026-06-12")

    # 只有老股 000009 的源起点滞后不适用：它缺的是"已有日线行但资金流为空"的内部洞。
    assert missing == {"000009"}


def test_read_capital_flow_missing_symbols_exempts_market_normal_suspension_days(tmp_path):
    """市场正常日的整行缺失（停牌）不算资金流缺口；thin day 的整行缺失照算。

    窗口 2026-06-01 ~ 2026-06-12（10 个交易日）：
    - 000010 只缺 06-03 一行，当日其余 4 只都有行（计数 4 ≥ 中位数 5 × 0.5）
      → 市场正常日 → 停牌类豁免，不进名单；
    - 000011/000013/000014 只缺 06-05 一行，而 06-05 全市场只剩 2 行
      （2 < 2.5 → thin day，疑似写入失败）→ 可行动，照算；
    - 000012 全窗口有行有流 → 不进名单。
    """
    warehouse = Warehouse(tmp_path)
    all_days = [
        "2026-06-01",
        "2026-06-02",
        "2026-06-03",
        "2026-06-04",
        "2026-06-05",
        "2026-06-08",
        "2026-06-09",
        "2026-06-10",
        "2026-06-11",
        "2026-06-12",
    ]
    thin_day = "2026-06-05"
    rows = []
    for symbol in ("000010", "000011", "000012", "000013", "000014"):
        for day in all_days:
            if symbol == "000010" and day == "2026-06-03":
                continue  # 000010 在市场正常日停牌
            if symbol in ("000011", "000013", "000014") and day == thin_day:
                continue  # 写入失败日：这三只的行整行丢失
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": day,
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "volume": 1000,
                    "main_net_inflow": 100.0,
                    "listing_days": 9999,
                }
            )
    warehouse.write_daily_bars(pd.DataFrame(rows))

    missing = warehouse.read_capital_flow_missing_symbols("2026-06-01", "2026-06-12")

    # 000010 的停牌豁免生效；thin day 丢行的三只照算；完整股 000012 不进名单。
    assert missing == {"000011", "000013", "000014"}


def test_read_capital_flow_missing_symbols_keeps_stale_tail_visible(tmp_path):
    """停更尾部不被停牌豁免吞掉：max(OHLC 末行, 资金流末行) 之后必须照算。

    000015 的数据停在 06-05，窗口尾部 06-08~06-12 全市场行数极低但仍被判为
    市场正常日（阈值下限 1.0）——这些日期既没有行也在尾部边界之后，属于
    "这只股票多久没同步"的可行动信号，绝不能按停牌豁免掉。
    """
    warehouse = Warehouse(tmp_path)
    stale_days = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"]
    all_days = [
        *stale_days,
        "2026-06-08",
        "2026-06-09",
        "2026-06-10",
        "2026-06-11",
        "2026-06-12",
    ]
    rows = [
        {
            "symbol": symbol,
            "trade_date": day,
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "volume": 1000,
            "main_net_inflow": 100.0,
            "listing_days": 9999,
        }
        for symbol, days in (("000015", stale_days), ("000016", all_days))
        for day in days
    ]
    warehouse.write_daily_bars(pd.DataFrame(rows))

    missing = warehouse.read_capital_flow_missing_symbols("2026-06-01", "2026-06-12")

    assert missing == {"000015"}


def test_read_capital_flow_missing_symbols_keeps_gaps_when_counts_unavailable(tmp_path, monkeypatch):
    """横截面计数拿不到 → 分类退空集 → 平日历口径：停牌豁免一个都不生效，缺口照算。

    000021 只缺 06-03 一行、该日在正常计数下会被判成市场正常日（本会被停牌豁免
    掉）；``market_trade_date_counts`` 抛异常时必须退回旧行为——把 06-03 当成普通
    缺口进名单，而不是因为分类失败把缺口静默吞掉（平日历 = 不豁免）。
    """
    warehouse = Warehouse(tmp_path)
    days = [
        "2026-06-01",
        "2026-06-02",
        "2026-06-03",
        "2026-06-04",
        "2026-06-05",
        "2026-06-08",
        "2026-06-09",
        "2026-06-10",
        "2026-06-11",
        "2026-06-12",
    ]
    rows = [
        {
            "symbol": symbol,
            "trade_date": day,
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "volume": 1000,
            "main_net_inflow": 100.0,
            "listing_days": 9999,
        }
        for symbol in ("000020", "000021")
        for day in days
        if not (symbol == "000021" and day == "2026-06-03")
    ]
    warehouse.write_daily_bars(pd.DataFrame(rows))

    # 计数可用：06-03 当天仍有 000020 的完整行 → 市场正常日 → 停牌豁免，名单为空。
    assert warehouse.read_capital_flow_missing_symbols("2026-06-01", "2026-06-12") == set()

    def _boom(self, start_date, end_date):
        raise RuntimeError("parquet scan failed")

    monkeypatch.setattr(Warehouse, "market_trade_date_counts", _boom)
    assert warehouse.read_capital_flow_missing_symbols("2026-06-01", "2026-06-12") == {"000021"}


def test_market_trade_date_counts_reports_ohlc_complete_rows_per_trade_date(tmp_path):
    """每个交易日的 OHLC 完整行数：四列全非空才计数，无完整行的交易日记 0。

    - 06-01：000001/000003 完整；000002 close 为空、000004 是资金流独立行
      （OHLC 全空）→ 2；
    - 06-02：只有 000001 → 1；
    - 06-03 是交易日但没有任何行 → 0（键来自交易日历，不是"有行的日期"）。
    """
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001", "000002", "000003", "000004", "000001"],
                "trade_date": ["2026-06-01", "2026-06-01", "2026-06-01", "2026-06-01", "2026-06-02"],
                "open": [10.0, 10.0, 10.0, float("nan"), 10.1],
                "high": [10.5, 10.5, 10.5, float("nan"), 10.6],
                "low": [9.8, 9.8, 9.8, float("nan"), 9.9],
                "close": [10.2, float("nan"), 10.2, float("nan"), 10.3],
                "volume": [1000, 1000, 1000, 0, 1100],
                "main_net_inflow": [10.0, float("nan"), float("nan"), 5.0, 12.0],
            }
        )
    )

    counts = warehouse.market_trade_date_counts("2026-06-01", "2026-06-03")

    assert counts == {"2026-06-01": 2, "2026-06-02": 1, "2026-06-03": 0}


def test_market_trade_date_counts_caches_by_window_until_written(tmp_path):
    """10 分钟 TTL 单条缓存，key 带日期区间，写入路径让它失效。"""
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001"],
                "trade_date": ["2026-06-01"],
                "open": [10.0],
                "high": [10.5],
                "low": [9.8],
                "close": [10.2],
                "volume": [1000],
            }
        )
    )

    first = warehouse.market_trade_date_counts("2026-06-01", "2026-06-03")
    assert warehouse.market_trade_date_counts("2026-06-01", "2026-06-03") is first

    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000002"],
                "trade_date": ["2026-06-02"],
                "open": [20.0],
                "high": [20.5],
                "low": [19.8],
                "close": [20.2],
                "volume": [2000],
            }
        )
    )

    second = warehouse.market_trade_date_counts("2026-06-01", "2026-06-03")
    assert second is not first  # 写入后失效并重算
    assert first["2026-06-02"] == 0
    assert second["2026-06-02"] == 1

    other_window = warehouse.market_trade_date_counts("2026-06-02", "2026-06-03")
    assert other_window is not second  # 缓存 key 带日期区间，换窗口不复用
    assert other_window == {"2026-06-02": 1, "2026-06-03": 0}


def test_market_trade_date_counts_skips_corrupt_partition(tmp_path):
    """坏分区跳过并登记 ``corrupt_partitions``，不让单个坏分区炸掉整个调用。"""
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001", "000001"],
                "trade_date": ["2025-12-30", "2025-12-31"],
                "open": [10.0, 10.1],
                "high": [10.5, 10.6],
                "low": [9.8, 9.9],
                "close": [10.2, 10.3],
                "volume": [1000, 1000],
            }
        )
    )
    corrupt = tmp_path / "warehouse" / "daily_bars" / "year=2026" / "daily_bars.parquet"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_bytes(b"not a parquet file")

    counts = warehouse.market_trade_date_counts("2025-12-30", "2026-01-05")

    # 应有交易日 = 12-30、12-31、01-05（01-01~01-03 元旦休市）；
    # year=2026 分区损坏 → 01-05 计 0，且调用不抛异常。
    assert counts == {"2025-12-30": 1, "2025-12-31": 1, "2026-01-05": 0}
    assert str(corrupt) in warehouse.corrupt_partitions


def test_classify_market_days_by_cross_section_splits_normal_and_thin_days():
    """模块级纯函数：行数 ≥ 中位数 × 比例 → 市场正常日，否则 thin day。"""
    from astock_backtester.data.warehouse import MarketDayClassification, classify_market_days_by_cross_section

    classification = classify_market_days_by_cross_section(
        {
            pd.Timestamp("2026-06-01"): 6,
            "2026-06-02": 1,
            pd.Timestamp("2026-06-03"): 4,
            pd.Timestamp("2026-06-04"): 6,
        }
    )

    assert isinstance(classification, MarketDayClassification)
    # 行数 6/1/4/6 → 中位数 5 → 阈值 2.5：06-02 thin，其余市场正常日；
    # 字符串键归一化成 pd.Timestamp。
    assert classification.thin_days == {pd.Timestamp("2026-06-02")}
    assert classification.market_normal_days == {
        pd.Timestamp("2026-06-01"),
        pd.Timestamp("2026-06-03"),
        pd.Timestamp("2026-06-04"),
    }

    strict = classify_market_days_by_cross_section(
        {
            pd.Timestamp("2026-06-01"): 6,
            pd.Timestamp("2026-06-02"): 1,
            pd.Timestamp("2026-06-03"): 4,
            pd.Timestamp("2026-06-04"): 6,
        },
        thin_day_ratio=1.0,
    )
    # 阈值 = 中位数 5：06-03（4 行）也落入 thin。
    assert pd.Timestamp("2026-06-03") in strict.thin_days
    assert pd.Timestamp("2026-06-01") in strict.market_normal_days

    empty = classify_market_days_by_cross_section({})
    assert empty.market_normal_days == set()
    assert empty.thin_days == set()


def test_coverage_delegates_cross_section_classification_to_module_function(tmp_path, monkeypatch):
    """coverage() 的横截面分类必须走模块级纯函数（sync/operations 复用同一口径）。"""
    import astock_backtester.data.warehouse as warehouse_module

    warehouse = warehouse_module.Warehouse(tmp_path)
    layout = {
        "000001": ("2026-06-01", "2026-06-02", "2026-06-03"),
        "000002": ("2026-06-01", "2026-06-03"),
        "000003": ("2026-06-01", "2026-06-03"),
    }
    warehouse.write_daily_bars(
        pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "trade_date": day,
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "volume": 1000,
                }
                for symbol, days in layout.items()
                for day in days
            ]
        )
    )

    seen: list[dict] = []
    original = warehouse_module.classify_market_days_by_cross_section

    def spy(rows_by_date, *, thin_day_ratio=0.5):
        seen.append(dict(rows_by_date))
        return original(rows_by_date, thin_day_ratio=thin_day_ratio)

    monkeypatch.setattr(warehouse_module, "classify_market_days_by_cross_section", spy)

    coverage = {item.dataset: item for item in warehouse.coverage()}

    assert seen
    assert {pd.Timestamp("2026-06-01"), pd.Timestamp("2026-06-03")} <= set(seen[0])
    # 行数 3/1/3 → 中位数 3 → 阈值 1.5 → 06-02 是 thin day：spanning 3 − 实有 1
    # = 2 计入 missing；06-01/06-03 无内部洞；三只都同步到 06-03 → 尾部 0。
    assert coverage["daily_bars"].missing_rows == 2
    assert coverage["daily_bars"].suspension_rows == 0
