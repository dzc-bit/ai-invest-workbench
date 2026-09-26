import logging
import threading
import time
from pathlib import Path

import pandas as pd
from astock_backtester.cli import handle_command
from astock_backtester.data.cache import LocalCache
from astock_backtester.data.importer import normalize_daily_bars
from astock_backtester.sample_data import sample_daily_bars


def test_normalize_daily_bars_accepts_required_columns():
    raw = sample_daily_bars().rename(columns={"trade_date": "date"})

    result = normalize_daily_bars(raw)

    assert result["trade_date"].dtype == "datetime64[ns]"
    assert result.columns.tolist()[:6] == ["symbol", "trade_date", "open", "high", "low", "close"]


def test_local_cache_round_trips_daily_bars(tmp_path):
    cache = LocalCache(tmp_path)
    bars = sample_daily_bars()

    cache.write_daily_bars(bars)
    loaded = cache.read_daily_bars()

    assert len(loaded) == len(bars)
    assert set(loaded["symbol"]) == {"AAA", "BBB"}


def test_local_cache_reports_dataset_coverage(tmp_path):
    cache = LocalCache(tmp_path)
    cache.write_daily_bars(sample_daily_bars())

    coverage = cache.coverage()

    daily = next(item for item in coverage if item.dataset == "daily_bars")
    assert daily.symbols == 2
    assert str(daily.start_date) == "2024-01-02"


def test_local_cache_logs_corrupt_daily_bars_parquet(tmp_path, caplog):
    cache = LocalCache(tmp_path)
    cache.daily_bars_path.write_bytes(b"not a parquet file")

    with caplog.at_level(logging.WARNING, logger="astock_backtester.data.cache"):
        loaded = cache.read_daily_bars()
        coverage = cache.coverage()

    assert loaded.empty
    assert all(item.symbols == 0 for item in coverage)
    assert "cached daily-bars parquet" in caplog.text


def test_cli_coverage_command_returns_daily_bars_dataset(tmp_path):
    payload = {"command": "coverage", "cache_dir": str(tmp_path)}
    response = handle_command(payload)

    assert response["ok"] is True
    assert response["coverage"][0]["dataset"] == "daily_bars"


def test_cli_import_daily_bars_sample_populates_cache(tmp_path):
    response = handle_command(
        {"command": "import_daily_bars", "source": "sample", "cache_dir": str(tmp_path)}
    )

    assert response["ok"] is True
    assert response["imported_rows"] == 10
    assert response["coverage"][0]["symbols"] == 2


def test_cli_import_daily_bars_file_populates_cache(tmp_path):
    source = tmp_path / "daily.csv"
    sample_daily_bars().to_csv(source, index=False)

    response = handle_command(
        {
            "command": "import_daily_bars",
            "source": "file",
            "path": str(source),
            "cache_dir": str(tmp_path / "cache"),
        }
    )

    assert response["ok"] is True
    assert response["imported_rows"] == 10
    assert response["coverage"][0]["start_date"] == "2024-01-02"


def test_cli_demo_backtest_returns_metrics():
    response = handle_command({"command": "demo_backtest"})

    assert response["ok"] is True
    assert response["result"]["metrics"]["trade_count"] >= 1
    assert response["result"]["trades"]


def test_cli_lists_condition_definitions_for_ui():
    response = handle_command({"command": "conditions"})

    assert response["ok"] is True
    ids = {item["condition_id"] for item in response["conditions"]}
    assert "macd_histogram_at_least" in ids
    assert "volume_ratio_between" in ids
    assert "capital_flow_n_day_sum_at_least" in ids


def test_cli_run_backtest_accepts_strategy_and_settings_payload():
    strategy = {
        "name": "ui supplied strategy",
        "market_filters": [
            {
                "id": "market",
                "condition_id": "market_rising_ratio_at_least",
                "enabled": True,
                "params": {"min_ratio": 0.5},
                "data_lag_days": 0,
            }
        ],
        "entry_groups": [
            {
                "id": "entry",
                "operator": "and",
                "conditions": [
                    {
                        "id": "cap",
                        "condition_id": "market_cap_between",
                        "enabled": True,
                        "params": {"min": 1, "max": 30_000_000_000},
                        "data_lag_days": 0,
                    },
                    {
                        "id": "vr",
                        "condition_id": "volume_ratio_between",
                        "enabled": True,
                        "params": {"window": 2, "min": 1.0, "max": 2.0},
                        "data_lag_days": 0,
                    },
                ],
            }
        ],
        "exit_rules": [
            {
                "id": "exit",
                "condition_id": "close_below_ma",
                "enabled": True,
                "params": {"window": 3},
                "data_lag_days": 0,
            }
        ],
        "score_threshold": None,
    }
    settings = {
        "start_date": "2024-01-02",
        "end_date": "2024-01-08",
        "initial_cash": 100000,
        "fixed_holding_days": 3,
        "take_profit_pct": 0.08,
        "stop_loss_pct": -0.05,
        "max_positions": 2,
        "max_daily_buys": 1,
    }

    response = handle_command({"command": "run_backtest", "strategy": strategy, "settings": settings})

    assert response["ok"] is True
    assert response["result"]["metrics"]["trade_count"] >= 1
    assert any("volume ratio" in reason for trade in response["result"]["trades"] for reason in trade["buy_reason"])


def test_cli_run_backtest_reports_empty_cache_when_cache_dir_is_explicit(tmp_path):
    strategy = {
        "name": "ui supplied strategy",
        "market_filters": [],
        "entry_groups": [
            {
                "id": "entry",
                "operator": "and",
                "conditions": [
                    {
                        "id": "cap",
                        "condition_id": "market_cap_between",
                        "enabled": True,
                        "params": {"min": 1, "max": 30_000_000_000},
                        "data_lag_days": 0,
                    }
                ],
            }
        ],
        "exit_rules": [],
        "score_threshold": None,
    }
    settings = {
        "start_date": "2024-01-02",
        "end_date": "2024-01-08",
        "initial_cash": 100000,
    }

    response = handle_command(
        {"command": "run_backtest", "cache_dir": str(tmp_path), "strategy": strategy, "settings": settings}
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "command_failed"
    assert "No cached daily bars found" in response["error"]["message"]


def test_cli_run_backtest_enriches_dynamic_condition_windows():
    strategy = {
        "name": "dynamic windows",
        "market_filters": [],
        "entry_groups": [
            {
                "id": "entry",
                "operator": "and",
                "conditions": [
                    {
                        "id": "cap",
                        "condition_id": "market_cap_between",
                        "enabled": True,
                        "params": {"min": 1, "max": 30_000_000_000},
                        "data_lag_days": 0,
                    },
                    {
                        "id": "gain",
                        "condition_id": "past_return_between",
                        "enabled": True,
                        "params": {"window": 4, "min": -0.5, "max": 0.5},
                        "data_lag_days": 0,
                    },
                    {
                        "id": "vr",
                        "condition_id": "volume_ratio_between",
                        "enabled": True,
                        "params": {"window": 4, "min": 0.1, "max": 3.0},
                        "data_lag_days": 0,
                    },
                ],
            }
        ],
        "exit_rules": [],
        "score_threshold": None,
    }
    settings = {
        "start_date": "2024-01-02",
        "end_date": "2024-01-08",
        "initial_cash": 100000,
        "fixed_holding_days": 3,
        "max_positions": 2,
        "max_daily_buys": 1,
    }

    response = handle_command({"command": "run_backtest", "strategy": strategy, "settings": settings})

    assert response["ok"] is True
    assert response["result"]["preflight_issues"] == []


def test_cli_rejects_unknown_command():
    response = handle_command({"command": "nope"})

    assert response["ok"] is False
    assert response["error"]["code"] == "unknown_command"


def test_local_cache_write_daily_bars_writes_via_temp_file_then_atomically_replaces(tmp_path, monkeypatch):
    cache = LocalCache(tmp_path)
    targets: list[Path] = []
    original_to_parquet = pd.DataFrame.to_parquet

    def spy(self, path, *args, **kwargs):
        targets.append(Path(path))
        return original_to_parquet(self, path, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", spy)

    cache.write_daily_bars(sample_daily_bars())

    assert targets, "to_parquet must be called"
    assert all(target != cache.daily_bars_path for target in targets)
    assert cache.daily_bars_path.exists()
    assert not list(cache.parquet_dir.glob("*.tmp"))
    assert len(cache.read_daily_bars()) == 10


def test_local_cache_write_daily_bars_acquires_cross_process_lock(tmp_path):
    cache = LocalCache(tmp_path)

    cache.write_daily_bars(sample_daily_bars())

    assert (cache.parquet_dir / "daily_bars.parquet.lock").exists()


def test_local_cache_concurrent_writes_leave_complete_readable_parquet(tmp_path, monkeypatch):
    cache = LocalCache(tmp_path)
    original_read = LocalCache.read_daily_bars

    def slow_read(self):
        frame = original_read(self)
        time.sleep(0.05)
        return frame

    monkeypatch.setattr(LocalCache, "read_daily_bars", slow_read)
    first = sample_daily_bars()
    second = sample_daily_bars().assign(symbol=lambda frame: frame["symbol"].map({"AAA": "CCC", "BBB": "DDD"}))
    errors: list[Exception] = []

    def write(frame: pd.DataFrame) -> None:
        try:
            cache.write_daily_bars(frame)
        except Exception as exc:  # noqa: BLE001 - surfaced via the errors list below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(frame,)) for frame in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    loaded = original_read(cache)
    assert set(loaded["symbol"]) == {"AAA", "BBB", "CCC", "DDD"}
    assert len(loaded) == len(first) + len(second)
    assert not list(cache.parquet_dir.glob("*.tmp"))
