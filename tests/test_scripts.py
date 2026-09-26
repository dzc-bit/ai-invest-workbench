from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path

import pandas as pd

# Merged from: test_capital_flow_backfill_script.py, test_full_market_import_script.py, test_build_scripts.py


# ---------------------------------------------------------------------------
# Capital flow backfill script tests
# ---------------------------------------------------------------------------


def load_backfill_script():
    script_path = Path(__file__).parents[1] / "scripts" / "run-capital-flow-backfill.py"
    spec = importlib.util.spec_from_file_location("run_capital_flow_backfill", script_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_select_backfill_symbols_prioritizes_missing_flow_rows(tmp_path):
    module = load_backfill_script()
    frame = pd.DataFrame(
        {
            "symbol": ["000001", "000001", "000002", "000003"],
            "trade_date": pd.to_datetime(["2026-06-05", "2026-06-06", "2026-06-05", "2026-06-05"]),
            "main_net_inflow": [1.0, float("nan"), float("nan"), 3.0],
        }
    )

    symbols = module.select_backfill_symbols(frame, explicit_symbols=[], completed_symbols=set(), limit=0)

    assert symbols == ["000001", "000002"]


def test_select_backfill_symbols_uses_explicit_symbols_when_daily_rows_are_missing(tmp_path):
    module = load_backfill_script()

    symbols = module.select_backfill_symbols(
        pd.DataFrame(),
        explicit_symbols=["1", "SZ000002", "600000.SH"],
        completed_symbols=set(),
        limit=2,
    )

    assert symbols == ["000001", "000002"]


def test_select_backfill_symbols_uses_provider_symbols_when_daily_rows_are_missing(tmp_path):
    module = load_backfill_script()

    symbols = module.select_backfill_symbols(
        pd.DataFrame(),
        explicit_symbols=[],
        completed_symbols={"000002"},
        limit=2,
        provider_symbols=["SZ000002", "600000.SH", "1"],
    )

    assert symbols == ["000001", "600000"]


def test_select_backfill_symbols_adds_provider_symbols_after_missing_daily_rows(tmp_path):
    module = load_backfill_script()
    frame = pd.DataFrame(
        {
            "symbol": ["000003", "000003"],
            "trade_date": pd.to_datetime(["2026-06-05", "2026-06-08"]),
            "main_net_inflow": [float("nan"), float("nan")],
        }
    )

    symbols = module.select_backfill_symbols(
        frame,
        explicit_symbols=[],
        completed_symbols=set(),
        limit=0,
        provider_symbols=["000001", "000003"],
    )

    assert symbols == ["000003", "000001"]


def test_load_provider_symbols_uses_configured_provider_without_daily_rows(monkeypatch):
    module = load_backfill_script()

    class FakeProvider:
        def list_symbols(self):
            return ["SZ000001", "600000.SH"]

    monkeypatch.setattr(module, "ADataProvider", lambda: object())
    monkeypatch.setattr(module, "AkshareProvider", lambda: object())
    monkeypatch.setattr(module, "HttpAStockProvider", lambda: object())
    monkeypatch.setattr(module, "CompositeProvider", lambda providers: FakeProvider())

    assert module.load_provider_symbols() == ["000001", "600000"]


def test_supervised_backfill_stops_after_consecutive_failures(tmp_path):
    module = load_backfill_script()
    events_path = tmp_path / "capital-flow-progress.jsonl"

    def failing_runner(symbol: str) -> dict:
        return {
            "status": "partial",
            "imported_rows": 0,
            "failures": [{"symbol": symbol, "code": "network_error", "message": "remote closed"}],
            "diagnostics": [],
        }

    summary = module.run_supervised_backfill(
        symbols=["000001", "000002", "000003"],
        run_symbol=failing_runner,
        progress_path=events_path,
        max_consecutive_failures=2,
        sleep_seconds=0,
    )

    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert summary["processed"] == 2
    assert summary["stopped_reason"] == "max_consecutive_failures"
    assert events[-1]["event"] == "stopped"
    assert events[-1]["consecutive_failures"] == 2


def test_supervised_backfill_treats_already_complete_result_as_completed(tmp_path):
    module = load_backfill_script()
    events_path = tmp_path / "capital-flow-progress.jsonl"

    def already_complete_runner(symbol: str) -> dict:
        return {
            "status": "ok",
            "imported_rows": 0,
            "failures": [],
            "diagnostics": [{"code": "capital_flow_backfill_not_needed"}],
        }

    summary = module.run_supervised_backfill(
        symbols=["000001"],
        run_symbol=already_complete_runner,
        progress_path=events_path,
        max_consecutive_failures=1,
        sleep_seconds=0,
    )

    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert summary["completed"] == 1
    assert summary["failed"] == 0
    assert summary["stopped_reason"] is None
    assert events[1]["status"] == "completed"
    assert events[1]["status_code"] == "ok"
    assert events[-1]["event"] == "finish"


def test_supervised_backfill_processes_symbols_in_batches_and_writes_resume_events(tmp_path):
    module = load_backfill_script()
    events_path = tmp_path / "capital-flow-progress.jsonl"
    calls = []

    def batch_runner(symbols: list[str]) -> dict:
        calls.append(list(symbols))
        return {
            "status": "ok",
            "imported_rows": len(symbols),
            "returned_rows": len(symbols) * 2,
            "fetched_symbols": symbols,
            "missing_symbols": [],
            "skipped_symbols": [],
            "failures": [],
            "diagnostics": [],
        }

    summary = module.run_supervised_backfill(
        symbols=["000001", "000002", "000003"],
        run_symbol=lambda symbol: None,
        run_batch=batch_runner,
        batch_size=2,
        progress_path=events_path,
        max_consecutive_failures=2,
        sleep_seconds=0,
    )

    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    completed_events = [event for event in events if event.get("status") == "completed"]
    assert calls == [["000001", "000002"], ["000003"]]
    assert summary["processed"] == 3
    assert summary["completed"] == 3
    assert summary["failed"] == 0
    assert summary["imported_rows"] == 3
    assert [event["symbol"] for event in completed_events] == ["000001", "000002", "000003"]
    assert all(event["completion_kind"] == "imported" for event in completed_events)
    assert module.load_completed_symbols(events_path) == {"000001", "000002", "000003"}


def test_supervised_backfill_batch_summary_uses_exact_imported_and_returned_rows(tmp_path):
    module = load_backfill_script()
    events_path = tmp_path / "capital-flow-progress.jsonl"

    def batch_runner(symbols: list[str]) -> dict:
        return {
            "status": "ok",
            "imported_rows": 7,
            "returned_rows": 9,
            "fetched_symbols": symbols,
            "missing_symbols": [],
            "skipped_symbols": [],
            "failures": [],
            "diagnostics": [],
        }

    summary = module.run_supervised_backfill(
        symbols=["000001", "000002"],
        run_symbol=lambda symbol: None,
        run_batch=batch_runner,
        batch_size=2,
        progress_path=events_path,
        max_consecutive_failures=2,
        sleep_seconds=0,
    )

    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    batch_events = [event for event in events if event.get("event") == "batch"]
    completed_events = [event for event in events if event.get("status") == "completed"]
    assert summary["imported_rows"] == 7
    assert summary["returned_rows"] == 9
    assert batch_events == [
        {
            "event": "batch",
            "time": batch_events[0]["time"],
            "index": 1,
            "total_batches": 1,
            "symbols": ["000001", "000002"],
            "status_code": "ok",
            "imported_rows": 7,
            "returned_rows": 9,
            "seconds": batch_events[0]["seconds"],
        }
    ]
    assert all(event["completion_kind"] == "imported" for event in completed_events)
    assert module.load_completed_symbols(events_path) == {"000001", "000002"}


def test_supervised_backfill_batch_symbol_events_use_per_symbol_row_counts(tmp_path):
    module = load_backfill_script()
    events_path = tmp_path / "capital-flow-progress.jsonl"

    def batch_runner(symbols: list[str]) -> dict:
        return {
            "status": "ok",
            "imported_rows": 5,
            "returned_rows": 7,
            "rows": [
                {"symbol": "000001", "trade_date": "2026-06-03", "main_net_inflow": 1.0},
                {"symbol": "000001", "trade_date": "2026-06-04", "main_net_inflow": 2.0},
                {"symbol": "000001", "trade_date": "2026-06-05", "main_net_inflow": 3.0},
                {"symbol": "000002", "trade_date": "2026-06-04", "main_net_inflow": 4.0},
                {"symbol": "000002", "trade_date": "2026-06-05", "main_net_inflow": 5.0},
            ],
            "diagnostics": [
                {
                    "code": "capital_flow_symbol_summary",
                    "symbol": "000001",
                    "returned_rows": 4,
                    "imported_rows": 3,
                },
                {
                    "code": "capital_flow_symbol_summary",
                    "symbol": "000002",
                    "returned_rows": 3,
                    "imported_rows": 2,
                },
            ],
            "fetched_symbols": symbols,
            "missing_symbols": [],
            "skipped_symbols": [],
            "failures": [],
        }

    summary = module.run_supervised_backfill(
        symbols=["000001", "000002"],
        run_symbol=lambda symbol: None,
        run_batch=batch_runner,
        batch_size=2,
        progress_path=events_path,
        max_consecutive_failures=2,
        sleep_seconds=0,
    )

    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    symbol_events = {
        event["symbol"]: event
        for event in events
        if event.get("status") == "completed"
    }
    assert summary["imported_rows"] == 5
    assert summary["returned_rows"] == 7
    assert symbol_events["000001"]["imported_rows"] == 3
    assert symbol_events["000001"]["returned_rows"] == 4
    assert symbol_events["000002"]["imported_rows"] == 2
    assert symbol_events["000002"]["returned_rows"] == 3


def test_supervised_backfill_batch_shortfall_marks_only_that_symbol_failed(tmp_path):
    module = load_backfill_script()
    events_path = tmp_path / "capital-flow-progress.jsonl"

    def batch_runner(symbols: list[str]) -> dict:
        return {
            "status": "partial",
            "imported_rows": 1,
            "returned_rows": 1,
            "fetched_symbols": ["000001"],
            "missing_symbols": ["000002"],
            "skipped_symbols": [],
            "failures": [],
            "diagnostics": [{"code": "date_coverage_shortfall", "symbol": "000002"}],
        }

    summary = module.run_supervised_backfill(
        symbols=["000001", "000002"],
        run_symbol=lambda symbol: None,
        run_batch=batch_runner,
        batch_size=2,
        progress_path=events_path,
        max_consecutive_failures=1,
        sleep_seconds=0,
    )

    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert summary["processed"] == 2
    assert summary["completed"] == 1
    assert summary["failed"] == 1
    assert summary["stopped_reason"] == "max_consecutive_failures"
    assert [event.get("status") for event in events if event.get("symbol") in {"000001", "000002"}] == [
        "completed",
        "failed",
    ]
    assert module.load_completed_symbols(events_path) == {"000001"}


def test_supervised_backfill_batch_partial_keeps_skipped_symbols_completed(tmp_path):
    module = load_backfill_script()
    events_path = tmp_path / "capital-flow-progress.jsonl"

    def batch_runner(symbols: list[str]) -> dict:
        return {
            "status": "partial",
            "imported_rows": 1,
            "returned_rows": 1,
            "fetched_symbols": ["000003"],
            "missing_symbols": ["000004"],
            "skipped_symbols": ["000001", "000002"],
            "failures": [{"symbol": "000004", "code": "network_error", "error": "remote closed"}],
            "diagnostics": [{"code": "capital_flow_crawler_fetch_summary", "skipped_symbols": ["000001", "000002"]}],
        }

    summary = module.run_supervised_backfill(
        symbols=["000001", "000002", "000003", "000004"],
        run_symbol=lambda symbol: None,
        run_batch=batch_runner,
        batch_size=4,
        progress_path=events_path,
        max_consecutive_failures=2,
        sleep_seconds=0,
    )

    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    symbol_statuses = {
        event["symbol"]: event["status"]
        for event in events
        if event.get("symbol") in {"000001", "000002", "000003", "000004"}
    }
    assert summary["processed"] == 4
    assert summary["completed"] == 3
    assert summary["failed"] == 1
    assert symbol_statuses == {
        "000001": "completed",
        "000002": "completed",
        "000003": "completed",
        "000004": "failed",
    }
    assert module.load_completed_symbols(events_path) == {"000001", "000002", "000003"}


def test_supervised_backfill_stops_on_shortfall_diagnostics_without_terminal_failures(tmp_path):
    module = load_backfill_script()
    events_path = tmp_path / "capital-flow-progress.jsonl"

    def partial_without_failures_runner(symbol: str) -> dict:
        return {
            "status": "partial",
            "imported_rows": 0,
            "failures": [],
            "diagnostics": [{"code": "date_coverage_shortfall", "symbol": symbol}],
        }

    summary = module.run_supervised_backfill(
        symbols=["000001", "000002"],
        run_symbol=partial_without_failures_runner,
        progress_path=events_path,
        max_consecutive_failures=1,
        sleep_seconds=0,
    )

    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert summary["processed"] == 1
    assert summary["completed"] == 0
    assert summary["failed"] == 1
    assert summary["stopped_reason"] == "max_consecutive_failures"
    assert events[1]["status"] == "failed"
    assert events[1]["diagnostics"][0]["code"] == "date_coverage_shortfall"
    assert events[-1]["event"] == "stopped"


def test_supervised_backfill_treats_sina_shortfall_as_completed_when_rows_return(tmp_path):
    module = load_backfill_script()
    events_path = tmp_path / "capital-flow-progress.jsonl"

    def sina_shortfall_runner(symbol: str) -> dict:
        return {
            "status": "ok",
            "imported_rows": 0,
            "returned_rows": 2770,
            "fetched_symbols": [symbol],
            "missing_symbols": [],
            "failures": [],
            "diagnostics": [{"code": "date_coverage_shortfall", "provider": "sina", "symbol": symbol}],
        }

    summary = module.run_supervised_backfill(
        symbols=["000001"],
        run_symbol=sina_shortfall_runner,
        progress_path=events_path,
        max_consecutive_failures=1,
        sleep_seconds=0,
    )

    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert summary["completed"] == 1
    assert summary["failed"] == 0
    assert events[1]["status"] == "completed"
    assert events[1]["returned_rows"] == 2770
    assert events[-1]["event"] == "finish"


def test_supervised_backfill_treats_known_source_gap_as_completed(tmp_path):
    module = load_backfill_script()
    events_path = tmp_path / "capital-flow-progress.jsonl"

    def known_gap_runner(symbol: str) -> dict:
        return {
            "status": "partial",
            "imported_rows": 0,
            "returned_rows": 2770,
            "fetched_symbols": [symbol],
            "missing_symbols": [],
            "failures": [],
            "diagnostics": [{"code": "capital_flow_known_source_gap_remaining", "symbol": symbol}],
        }

    summary = module.run_supervised_backfill(
        symbols=["000001"],
        run_symbol=known_gap_runner,
        progress_path=events_path,
        max_consecutive_failures=1,
        sleep_seconds=0,
    )

    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert summary["completed"] == 1
    assert summary["failed"] == 0
    assert events[1]["completion_kind"] == "known_source_gap"
    assert module.load_completed_symbols(events_path) == {"000001"}


def test_supervised_backfill_batch_does_not_complete_symbol_missing_from_result(tmp_path):
    module = load_backfill_script()
    events_path = tmp_path / "capital-flow-progress.jsonl"

    def batch_runner(symbols: list[str]) -> dict:
        return {
            "status": "ok",
            "imported_rows": 1,
            "returned_rows": 1,
            "fetched_symbols": ["000001"],
            "missing_symbols": [],
            "skipped_symbols": [],
            "failures": [],
            "diagnostics": [],
        }

    summary = module.run_supervised_backfill(
        symbols=["000001", "000002"],
        run_symbol=lambda symbol: None,
        run_batch=batch_runner,
        batch_size=2,
        progress_path=events_path,
        max_consecutive_failures=1,
        sleep_seconds=0,
    )

    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    symbol_statuses = {
        event["symbol"]: event["status"]
        for event in events
        if event.get("symbol") in {"000001", "000002"}
    }
    assert summary["completed"] == 1
    assert summary["failed"] == 1
    assert summary["stopped_reason"] == "max_consecutive_failures"
    assert symbol_statuses == {"000001": "completed", "000002": "failed"}
    assert module.load_completed_symbols(events_path) == {"000001"}


def test_load_completed_symbols_resumes_not_needed_symbols(tmp_path):
    module = load_backfill_script()
    progress_path = tmp_path / "capital-flow-progress.jsonl"
    progress_path.write_text(
        "\n".join(
            [
                json.dumps({"status": "completed", "symbol": "000001", "imported_rows": 3}),
                json.dumps(
                    {
                        "status": "completed",
                        "symbol": "000002",
                        "imported_rows": 0,
                        "completion_kind": "already_complete",
                    }
                ),
                json.dumps({"status": "failed", "symbol": "000003", "imported_rows": 0}),
            ]
        ),
        encoding="utf-8",
    )

    assert module.load_completed_symbols(progress_path) == {"000001", "000002"}


def test_load_completed_symbols_does_not_resume_returned_only_symbols(tmp_path):
    module = load_backfill_script()
    progress_path = tmp_path / "capital-flow-progress.jsonl"
    progress_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "status": "completed",
                        "symbol": "000001",
                        "imported_rows": 0,
                        "returned_rows": 2770,
                        "completion_kind": "imported",
                    }
                ),
                json.dumps(
                    {
                        "status": "completed",
                        "symbol": "000002",
                        "imported_rows": 0,
                        "returned_rows": 0,
                        "completion_kind": "imported",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    assert module.load_completed_symbols(progress_path) == set()


def test_run_backfill_from_args_passes_crawler_tuning_options(monkeypatch, tmp_path):
    module = load_backfill_script()
    calls = []

    class FakeCache:
        def __init__(self, root):
            self.root = root

        def read_daily_bars(self):
            return pd.DataFrame()

    class FakeWarehouse:
        def __init__(self, root):
            self.root = root

        def read_daily_bars(self, *args, **kwargs):
            return pd.DataFrame()

    class FakeCrawler:
        def fetch_many_fund_flows(self, symbols, start_date, end_date, **kwargs):
            calls.append((list(symbols), start_date, end_date, kwargs))
            return {
                "rows": [
                    {"symbol": symbols[0], "trade_date": "2026-06-05", "main_net_inflow": 1.0}
                ],
                "failures": [],
                "diagnostics": [],
            }

    def fake_fetch_capital_flow_into_cache(**kwargs):
        result = kwargs["capital_flow_fetcher"](kwargs["symbols"], kwargs["start_date"], kwargs["end_date"])
        assert result["rows"]
        return {
            "status": "ok",
            "imported_rows": 1,
            "returned_rows": 1,
            "fetched_symbols": kwargs["symbols"],
            "missing_symbols": [],
            "skipped_symbols": [],
            "failures": [],
            "diagnostics": [
                {
                    "code": "capital_flow_symbol_summary",
                    "symbol": kwargs["symbols"][0],
                    "imported_rows": 1,
                    "returned_rows": 1,
                }
            ],
        }

    monkeypatch.setattr(module, "LocalCache", FakeCache)
    monkeypatch.setattr(module, "Warehouse", FakeWarehouse)
    monkeypatch.setattr(module, "CapitalFlowCrawler", lambda: FakeCrawler())
    monkeypatch.setattr(module, "fetch_capital_flow_into_cache", fake_fetch_capital_flow_into_cache)

    summary = module.run_backfill_from_args(
        [
            "--cache-dir",
            str(tmp_path),
            "--symbols",
            "000001",
            "--start-date",
            "2026-06-05",
            "--end-date",
            "2026-06-05",
            "--batch-size",
            "1",
            "--sleep-seconds",
            "0",
            "--max-workers",
            "8",
            "--timeout",
            "7",
            "--skip-eastmoney",
            "--no-provider-symbols",
        ]
    )

    assert summary["completed"] == 1
    assert calls == [
        (
            ["000001"],
            "2026-06-05",
            "2026-06-05",
            {"skip_eastmoney": True, "timeout": 7, "max_workers": 8},
        )
    ]


def test_select_missing_symbols_from_warehouse_reads_minimal_ohlc_gaps(tmp_path):
    module = load_backfill_script()
    warehouse = module.Warehouse(tmp_path)
    warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001", "000002", "000003", "000004"],
                "trade_date": ["2026-06-05", "2026-06-05", "2026-06-05", "2025-12-31"],
                "open": [10.0, 10.0, float("nan"), 10.0],
                "high": [10.5, 10.5, float("nan"), 10.5],
                "low": [9.8, 9.8, float("nan"), 9.8],
                "close": [10.2, 10.2, float("nan"), 10.2],
                "volume": [1000, 1000, 0, 1000],
                "main_net_inflow": [float("nan"), 1.0, 2.0, float("nan")],
            }
        )
    )

    symbols = module.select_missing_symbols_from_warehouse(
        warehouse,
        completed_symbols={"000001"},
        start_date="2025-01-01",
        end_date="2026-06-08",
        limit=1,
    )

    assert symbols == ["000004"]


def test_run_backfill_from_args_does_not_append_provider_symbols_to_local_gaps(monkeypatch, tmp_path):
    module = load_backfill_script()
    selected_batches = []

    class FakeCache:
        def __init__(self, root):
            self.root = root

        def read_daily_bars(self):
            return pd.DataFrame()

    class FakeWarehouse:
        def __init__(self, root):
            self.root = root

        def read_daily_bars(self, *args, **kwargs):
            return pd.DataFrame()

    monkeypatch.setattr(module, "LocalCache", FakeCache)
    monkeypatch.setattr(module, "Warehouse", FakeWarehouse)
    monkeypatch.setattr(
        module,
        "select_missing_symbols_from_warehouse",
        lambda warehouse, completed, start, end, limit: ["000001"],
    )
    monkeypatch.setattr(module, "load_provider_symbols", lambda: ["000001", "000002"])
    monkeypatch.setattr(module, "CapitalFlowCrawler", lambda: object())

    def fake_fetch_capital_flow_into_cache(**kwargs):
        selected_batches.append(list(kwargs["symbols"]))
        return {
            "status": "ok",
            "imported_rows": 1,
            "returned_rows": 1,
            "fetched_symbols": kwargs["symbols"],
            "missing_symbols": [],
            "skipped_symbols": [],
            "failures": [],
            "diagnostics": [],
        }

    monkeypatch.setattr(module, "fetch_capital_flow_into_cache", fake_fetch_capital_flow_into_cache)

    summary = module.run_backfill_from_args(
        [
            "--cache-dir",
            str(tmp_path),
            "--limit",
            "2",
            "--sleep-seconds",
            "0",
        ]
    )

    assert summary["total_symbols"] == 1
    assert selected_batches == [["000001"]]


# ---------------------------------------------------------------------------
# Full market import script tests
# ---------------------------------------------------------------------------


def load_import_script():
    script_path = Path(__file__).parents[1] / "scripts" / "run-full-market-import.py"
    spec = importlib.util.spec_from_file_location("run_full_market_import", script_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BrokenProvider:
    def list_symbols(self) -> list[str]:
        raise RuntimeError("remote symbol list failed")


class EmptyDailyProvider:
    def fetch_daily_bars(self, symbol, start_date, end_date):
        return pd.DataFrame()


def test_load_symbols_falls_back_to_adata_local_cache(tmp_path, monkeypatch):
    module = load_import_script()
    monkeypatch.setattr(module, "read_adata_local_symbols", lambda: ["1", "SZ000002", "600000.SH"])

    symbols, source = module.load_symbols(tmp_path, BrokenProvider())

    assert symbols == ["000001", "000002", "600000"]
    assert source == "adata-local"
    assert (tmp_path / "symbols.csv").exists()


def test_load_symbols_uses_existing_cache_when_provider_is_broken(tmp_path):
    module = load_import_script()
    (tmp_path / "symbols.csv").write_text("symbol\n000001\n000002\n", encoding="utf-8")

    symbols, source = module.load_symbols(tmp_path, BrokenProvider())

    assert symbols == ["000001", "000002"]
    assert source == "cache"


def test_fetch_daily_bars_falls_back_to_http_source(monkeypatch):
    module = load_import_script()

    class FakeAdapter:
        def fetch_daily_bars(self, symbols, start_date, end_date):
            return pd.DataFrame(
                {
                    "symbol": symbols,
                    "trade_date": [start_date],
                    "open": [1.0],
                    "high": [1.0],
                    "low": [1.0],
                    "close": [1.0],
                    "volume": [1],
                }
            )

    class FakeAdapterFactory:
        @classmethod
        def from_http_sources(cls):
            return FakeAdapter()

    monkeypatch.setattr(module, "AStockDataAdapter", FakeAdapterFactory)

    frame, source = module.fetch_daily_bars("000001", "2024-01-02", "2024-01-02", EmptyDailyProvider())

    assert source == "http"
    assert frame.loc[0, "symbol"] == "000001"


def test_resume_only_counts_completed_symbols_with_rows(tmp_path):
    module = load_import_script()
    progress_path = tmp_path / "import-progress.jsonl"
    progress_path.write_text(
        "\n".join(
            [
                '{"status":"completed","symbol":"000001","rows":0}',
                '{"status":"completed","symbol":"000002","rows":12}',
                '{"status":"failed","symbol":"000003","rows":12}',
            ]
        ),
        encoding="utf-8",
    )

    completed = module.load_completed_symbols(progress_path)

    assert completed == {"000002"}


# ---------------------------------------------------------------------------
# Build scripts tests
# ---------------------------------------------------------------------------


def test_build_data_service_prefers_bundled_python_before_system_python():
    script = Path("scripts/build-data-service.ps1").read_text(encoding="utf-8")

    bundled_python = '.tools\\python-3.11.9\\python.exe'
    assert bundled_python in script
    assert script.index(bundled_python) < script.index('$Python = "python"')


def test_build_data_service_targets_tauri_bin_executable():
    script = Path("scripts/build-data-service.ps1").read_text(encoding="utf-8")

    assert 'Join-Path $repoRoot "src-tauri\\bin"' in script
    assert 'Join-Path $distDir "astock-data-service.exe"' in script
    assert 'Join-Path $distDir "node.exe"' in script
    assert 'Join-Path $distDir "ths-cookie-worker.cjs"' in script
    assert 'Join-Path $distDir "xhr-sync-worker.js"' in script
    assert "--distpath $distDir" in script


def test_build_data_service_bundles_ths_cookie_worker_for_desktop_score():
    script = Path("scripts/build-data-service.ps1").read_text(encoding="utf-8")

    assert "esbuild" in script
    assert "jsdom" in script
    assert "THS_COOKIE_TIMEOUT_MS" in script
    assert "setTimeout(resolve, 25)" in script
    assert "setTimeout(resolve, 3000)" not in script


def test_build_data_service_collects_curl_cffi_native_dependencies():
    script = Path("scripts/build-data-service.ps1").read_text(encoding="utf-8")

    assert "--collect-all curl_cffi" in script
    assert "--hidden-import curl_cffi.requests" in script


def test_tauri_bundle_builds_data_service_before_packaging():
    config = json.loads(Path("src-tauri/tauri.conf.json").read_text(encoding="utf-8"))

    before_build = config["build"]["beforeBuildCommand"]
    assert before_build == (
        ".\\.tools\\node-v20.18.1-win-x64\\npm.cmd run build && "
        ".\\.tools\\node-v20.18.1-win-x64\\npm.cmd run build:data-service"
    )
    assert config["bundle"]["resources"] == ["bin"]


def test_vite_config_resolves_real_frontend_root_from_config_dir():
    config = Path("frontend/vite.config.ts").read_text(encoding="utf-8")

    assert 'const frontendRoot = realpathSync(fileURLToPath(new URL(".", import.meta.url)));' in config
    assert "root: frontendRoot" in config
    assert 'outDir: resolve(frontendRoot, "../dist")' in config


def test_write_latest_json_supports_distinct_release_asset_name():
    script = Path("scripts/write-latest-json.ps1").read_text(encoding="utf-8")

    assert "[string]$ReleaseAssetName = \"\"" in script
    assert "if (-not $ReleaseAssetName)" in script
    assert "$ReleaseAssetName = $AssetName" in script
    assert "[string]$RepoSlug = \"dzc-bit/ai-invest-workbench\"" in script
    assert "url = \"https://github.com/$RepoSlug/releases/download/$Tag/$ReleaseAssetName\"" in script


def test_write_latest_json_guards_against_stale_signature_reuse():
    """旧签名复用防线：.sig 内嵌 file 名必须与 -AssetName 一致、mtime 不得早于
    安装包、-Version 缺省读 package.json（bundle/nsis 里堆着多个历史版本的
    exe+sig，传错 -AssetName 就会把旧 .sig 配上新版本号——AGENTS.md §12 明令禁止）。"""
    script = Path("scripts/write-latest-json.ps1").read_text(encoding="utf-8")

    assert "if (-not $Version) {" in script
    assert "$packageJson.version" in script
    assert "signature was generated for" in script
    assert "stale signature reuse" in script
    assert "signature file is older than the installer" in script


def test_service_manager_defines_and_uses_packaged_sidecar_relative_helper():
    source = Path("src-tauri/src/service_manager.rs").read_text(encoding="utf-8")

    assert "fn packaged_service_relative_path() -> PathBuf" in source
    assert "resource_dir.join(packaged_service_relative_path())" in source


def test_service_manager_resolves_release_cache_dir_from_d_drive_workspace_data_dir():
    source = Path("src-tauri/src/service_manager.rs").read_text(encoding="utf-8")

    assert 'join("运行产物").join("本地数据仓")' in source
    assert "app_local_data_dir()" not in source


def test_release_manifests_use_one_version():
    package_version = json.loads(Path("package.json").read_text(encoding="utf-8"))["version"]
    package_lock = json.loads(Path("package-lock.json").read_text(encoding="utf-8"))
    package_lock_version = package_lock["version"]
    package_lock_root_version = package_lock["packages"][""]["version"]
    python_version = tomllib.loads(
        Path("pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    cargo_version = tomllib.loads(
        Path("src-tauri/Cargo.toml").read_text(encoding="utf-8")
    )["package"]["version"]
    tauri_version = json.loads(
        Path("src-tauri/tauri.conf.json").read_text(encoding="utf-8")
    )["version"]

    # Also check the Python package __version__ attribute.
    init_text = Path("backend/astock_backtester/__init__.py").read_text(encoding="utf-8")
    init_version = None
    for line in init_text.splitlines():
        if line.startswith("__version__"):
            init_version = line.split("=", 1)[1].strip().strip("\"'")
            break
    assert init_version is not None, "__version__ not found in __init__.py"

    all_versions = {
        package_version,
        package_lock_version,
        package_lock_root_version,
        python_version,
        cargo_version,
        tauri_version,
        init_version,
    }
    # 不再硬编码具体版本号：每次升版都要手改这里，而文档从没写——
    # 版本一致性以 package.json 为锚点做跨文件校验。
    assert all_versions == {package_version}


def test_deprecated_full_array_strategy_mutation_is_removed():
    frontend = Path("frontend/src/savedStrategies.ts").read_text(encoding="utf-8")
    commands = Path("src-tauri/src/commands.rs").read_text(encoding="utf-8")
    tauri_lib = Path("src-tauri/src/lib.rs").read_text(encoding="utf-8")
    production = "\n".join((frontend, commands, tauri_lib))

    deprecated_command = "_".join(("persist", "saved", "strategies"))
    deprecated_helper = "persist" + "SavedStrategiesToStore"
    assert deprecated_command not in production
    assert deprecated_helper not in production
    assert "upsert_saved_strategy" in production
    assert "delete_saved_strategy" in production


def test_gitignore_keeps_test_log_negation_rule():
    """``*.log`` 兜底不能静默吞掉测试 fixture：``!tests/**/*.log`` 必须一直存在且排在它之后。

    gitignore 后写的规则覆盖先写的：否定规则被删、或被挪到 ``*.log`` 之前都会失效，
    未来测试树里一旦出现 .log 扩展名的 fixture，就会变成"测试跑得过、git status
    看不见"的隐形文件。守卫它，避免有人顺手清理注释时把规则一起删掉。
    """
    lines = [line.strip() for line in Path(".gitignore").read_text(encoding="utf-8").splitlines()]
    wildcard_logs = [index for index, line in enumerate(lines) if line == "*.log"]
    negations = [index for index, line in enumerate(lines) if line == "!tests/**/*.log"]

    assert negations, ".gitignore 缺少 !tests/**/*.log 否定规则：测试 fixture 会被 *.log 兜底静默忽略"
    assert not wildcard_logs or negations[-1] > wildcard_logs[-1], "否定规则必须排在 *.log 之后才生效"


# ---------------------------------------------------------------------------
# Market cap backfill script tests
# ---------------------------------------------------------------------------


def load_market_cap_script():
    script_path = Path(__file__).parents[1] / "scripts" / "backfill-market-cap.py"
    spec = importlib.util.spec_from_file_location("backfill_market_cap", script_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _market_cap_rows(symbol: str = "000001") -> pd.DataFrame:
    """3 行：前两行市值缺失、第三行已有市值（必须原样保留）。"""
    return pd.DataFrame(
        {
            "symbol": [symbol, symbol, symbol],
            "trade_date": ["2026-06-05", "2026-06-08", "2026-06-09"],
            "open": [10.0, 11.0, 12.0],
            "high": [10.5, 11.5, 12.5],
            "low": [9.8, 10.8, 11.8],
            "close": [10.0, 11.0, 12.0],
            "volume": [1000, 1100, 1200],
            "amount": [12345.0, 23456.0, 34567.0],
            "float_market_cap": [float("nan"), float("nan"), 555.0],
            "total_market_cap": [float("nan"), 999.0, 888.0],
        }
    )


def _share_history() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "stock_code": ["000001"],
            "change_date": ["2026-01-01"],
            "total_shares": [2000],
            "list_a_shares": [1000],
        }
    )


def test_backfill_market_cap_fills_null_rows_and_preserves_existing_columns(tmp_path):
    """回填 = 整行回写：市值按股本历史算，其余列（含已有总市值）一律保持原值。

    ``write_daily_bars`` 先跑 ``normalize_daily_bars``：只写市值列会给缺失可选列
    填默认值（amount=0.0 等），这些非空默认值会经 ``combine_first`` 覆盖真实数据。
    """
    module = load_market_cap_script()
    warehouse = module.Warehouse(tmp_path)
    warehouse.write_daily_bars(_market_cap_rows())

    result = module.backfill_market_cap(warehouse, share_fetcher=lambda _code: _share_history())

    assert result["filled"] == 2
    assert result["skipped"] == 0
    assert result["failed"] == 0
    assert result["filled_symbols"] == ["000001"]

    loaded = warehouse.read_daily_bars(symbols=["000001"]).sort_values("trade_date").reset_index(drop=True)
    # 市值 = list_a_shares × close（merge_asof backward 把变动日快照对齐到交易日）
    assert loaded["float_market_cap"].tolist() == [10000.0, 11000.0, 555.0]
    assert loaded["amount"].tolist() == [12345.0, 23456.0, 34567.0]
    assert loaded["close"].tolist() == [10.0, 11.0, 12.0]
    assert loaded["volume"].tolist() == [1000, 1100, 1200]
    # adata 推导的 total_market_cap 不进写入帧：原值保留，原 null 仍是 null
    assert pd.isna(loaded.loc[0, "total_market_cap"])
    assert loaded["total_market_cap"].tolist()[1:] == [999.0, 888.0]


def test_backfill_market_cap_dry_run_counts_without_writing(tmp_path):
    module = load_market_cap_script()
    warehouse = module.Warehouse(tmp_path)
    warehouse.write_daily_bars(_market_cap_rows())

    result = module.backfill_market_cap(warehouse, dry_run=True, share_fetcher=lambda _code: _share_history())

    assert result["filled"] == 2
    loaded = warehouse.read_daily_bars(symbols=["000001"])
    assert loaded["float_market_cap"].isna().tolist() == [True, True, False], "dry-run 不得写库"


def test_backfill_market_cap_skips_symbols_without_usable_share_history(tmp_path):
    """股本历史为空、缺必需列、或 list_a_shares 全空 → 计入 skipped，不写库。"""
    module = load_market_cap_script()
    unusable = {
        "empty": pd.DataFrame(),
        "no_column": pd.DataFrame({"change_date": ["2026-01-01"], "total_shares": [1000]}),
        "all_null": pd.DataFrame({"change_date": ["2026-01-01"], "list_a_shares": [float("nan")]}),
    }

    for label, shares in unusable.items():
        warehouse = module.Warehouse(tmp_path / label)
        warehouse.write_daily_bars(_market_cap_rows())
        result = module.backfill_market_cap(warehouse, share_fetcher=lambda _code, _shares=shares: _shares)

        assert result["filled"] == 0, label
        assert result["skipped"] == 1, label
        assert result["failed"] == 0, label
        assert result["skipped_symbols"] == ["000001"], label
        loaded = warehouse.read_daily_bars(symbols=["000001"])
        assert loaded["float_market_cap"].isna().tolist()[:2] == [True, True], label


def test_backfill_market_cap_keeps_going_after_a_share_history_failure(tmp_path):
    """单股票取股本历史抛异常 → 计入 failed 且不中断其余股票。"""
    module = load_market_cap_script()
    warehouse = module.Warehouse(tmp_path)
    warehouse.write_daily_bars(pd.concat([_market_cap_rows("000001"), _market_cap_rows("000002")], ignore_index=True))

    def fetcher(code):
        if code == "000002":
            raise RuntimeError("adata exploded")
        return _share_history()

    result = module.backfill_market_cap(warehouse, share_fetcher=fetcher)

    assert result["failed"] == 1
    assert result["failed_symbols"] == ["000002"]
    assert result["filled"] == 2
    assert result["filled_symbols"] == ["000001"]
    skipped = warehouse.read_daily_bars(symbols=["000002"])
    assert skipped["float_market_cap"].isna().tolist() == [True, True, False]


def test_backfill_market_cap_filters_by_explicit_symbols(tmp_path):
    module = load_market_cap_script()
    warehouse = module.Warehouse(tmp_path)
    warehouse.write_daily_bars(pd.concat([_market_cap_rows("000001"), _market_cap_rows("000002")], ignore_index=True))

    result = module.backfill_market_cap(warehouse, symbols=["SZ000002"], share_fetcher=lambda _code: _share_history())

    assert result["filled"] == 2
    assert result["filled_symbols"] == ["000002"]
    untouched = warehouse.read_daily_bars(symbols=["000001"])
    assert untouched["float_market_cap"].isna().tolist() == [True, True, False]


def test_backfill_market_cap_cli_dry_run_prints_summary(monkeypatch, tmp_path, capsys):
    module = load_market_cap_script()
    cache_dir = tmp_path / "仓"
    module.Warehouse(cache_dir).write_daily_bars(_market_cap_rows())
    monkeypatch.setattr(module, "fetch_share_history", lambda _code: _share_history())
    monkeypatch.setattr(
        sys,
        "argv",
        ["backfill-market-cap.py", "--cache-dir", str(cache_dir), "--dry-run"],
    )

    module.main()

    output = capsys.readouterr().out
    assert "filled=2 rows" in output
    assert "skipped=0 symbols" in output
    assert "dry-run" in output
    loaded = module.Warehouse(cache_dir).read_daily_bars(symbols=["000001"])
    assert loaded["float_market_cap"].isna().tolist() == [True, True, False]
