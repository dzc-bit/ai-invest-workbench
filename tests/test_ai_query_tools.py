from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
from astock_backtester.ai.tools.query_tools import build_query_tools
from astock_backtester.ai.tools.registry import ToolRegistry
from test_ai_tools import FakeBackend, _bars_frame


def _write_parquet_partition(root: Path, year: int, rows: list[dict[str, Any]]) -> None:
    partition = root / f"year={year}"
    partition.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(partition / "daily_bars.parquet", index=False)


def _registry(backend: FakeBackend) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_all(build_query_tools(backend))
    return registry


def test_query_warehouse_sql_blocks_file_primitives(tmp_path):
    rows = [{"symbol": "600519", "trade_date": pd.Timestamp("2026-01-05"), "close": 1500.0}]
    _write_parquet_partition(tmp_path, 2026, rows)
    secret = tmp_path / "secret.txt"
    secret.write_text("top-secret", encoding="utf-8")
    backend = FakeBackend()
    backend.warehouse.parquet_paths = [str(tmp_path / "year=2026" / "daily_bars.parquet")]
    registry = _registry(backend)

    escaped = str(secret).replace("\\", "\\\\")
    read_text = registry.execute(
        "query_warehouse_sql", '{"sql": "SELECT content FROM read_text(\'' + escaped + '\')"}'
    )
    assert read_text.ok is False and "只允许" in read_text.summary
    glob_call = registry.execute("query_warehouse_sql", '{"sql": "SELECT * FROM glob(\'**/*.json\')"}')
    assert glob_call.ok is False
    path_literal = registry.execute(
        "query_warehouse_sql", '{"sql": "SELECT 1 WHERE x = \'' + escaped + '\'"}'
    )
    assert path_literal.ok is False
    # 正常值字面量不受影响
    normal = registry.execute(
        "query_warehouse_sql",
        '{"sql": "SELECT * FROM daily_bars WHERE symbol = \'600519\' AND trade_date >= TIMESTAMP \'2026-01-01\'"}',
    )
    assert normal.ok is True


def test_query_warehouse_sql_select_and_guard(tmp_path):
    rows = [
        {"symbol": "600519", "stock_name": "贵州茅台", "trade_date": pd.Timestamp("2026-01-05"), "close": 1500.0, "change_pct": 0.01},
        {"symbol": "000001", "stock_name": "平安银行", "trade_date": pd.Timestamp("2026-01-05"), "close": 11.5, "change_pct": -0.02},
        {"symbol": "600519", "stock_name": "贵州茅台", "trade_date": pd.Timestamp("2026-01-06"), "close": 1515.0, "change_pct": 0.01},
    ]
    _write_parquet_partition(tmp_path, 2026, rows)
    backend = FakeBackend()
    backend.warehouse.parquet_paths = [str(tmp_path / "year=2026" / "daily_bars.parquet")]
    registry = _registry(backend)

    select = registry.execute(
        "query_warehouse_sql",
        '{"sql": "SELECT symbol, close FROM daily_bars WHERE close > 100 ORDER BY close DESC"}',
    )
    assert select.ok is True
    assert select.payload["row_count"] == 2
    assert select.payload["rows"][0]["symbol"] == "600519"
    assert "查询成功 2 行" in select.summary

    update = registry.execute("query_warehouse_sql", '{"sql": "UPDATE daily_bars SET close = 0"}')
    assert update.ok is False and "只允许" in update.summary
    delete = registry.execute("query_warehouse_sql", '{"sql": "DELETE FROM daily_bars"}')
    assert delete.ok is False
    attach = registry.execute("query_warehouse_sql", '{"sql": "SELECT 1; ATTACH \'x.db\' AS x"}')
    assert attach.ok is False
    create = registry.execute("query_warehouse_sql", '{"sql": "CREATE TABLE t(a int)"}')
    assert create.ok is False


def test_query_warehouse_sql_auto_limit_and_empty_warehouse(tmp_path):
    rows = [{"symbol": "600519", "trade_date": pd.Timestamp("2026-01-05"), "close": 1500.0}]
    _write_parquet_partition(tmp_path, 2026, rows)
    backend = FakeBackend()
    backend.warehouse.parquet_paths = [str(tmp_path / "year=2026" / "daily_bars.parquet")]
    registry = _registry(backend)
    execution = registry.execute("query_warehouse_sql", '{"sql": "SELECT * FROM daily_bars"}')
    assert execution.ok is True
    assert execution.payload["row_count"] == 1

    empty = FakeBackend()
    empty.warehouse.parquet_paths = []
    empty_registry = _registry(empty)
    missing = empty_registry.execute("query_warehouse_sql", '{"sql": "SELECT 1"}')
    assert missing.ok is False and "没有" in missing.summary

    no_warehouse = FakeBackend()
    no_warehouse.warehouse.parquet_paths = [str(tmp_path / "year=2026" / "daily_bars.parquet")]
    broken = _registry(no_warehouse).execute(
        "query_warehouse_sql", '{"sql": "SELECT undefined_table.* FROM daily_bars"}'
    )
    assert broken.ok is False


def test_compute_stock_stats_returns_metrics():
    backend = FakeBackend()
    backend.frame = _bars_frame()
    registry = _registry(backend)
    execution = registry.execute("compute_stock_stats", '{"symbol": "600519", "window": 30}')
    assert execution.ok is True
    stats = execution.payload["stats"]
    assert stats["window_days"] == 30
    assert stats["first_close"] == 100.5
    assert stats["last_close"] == 129.5
    assert stats["main_net_inflow_sum"] is not None
    assert "区间收益" in execution.summary
    empty_backend = FakeBackend()
    missing = _registry(empty_backend).execute("compute_stock_stats", '{"symbol": "600519"}')
    assert missing.ok is False


def test_update_stock_data_routes_through_operations(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_fetch(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(
            status="ok",
            imported_rows=120,
            fetched_symbols=["600519"],
            missing_symbols=[],
            skipped_symbols=[],
            failures=[],
            logs=[SimpleNamespace(level="info", message="补齐完成")],
        )

    import astock_backtester.data.operations as operations

    monkeypatch.setattr(operations, "fetch_daily_bars_into_cache", fake_fetch)
    backend = FakeBackend()
    registry = _registry(backend)
    execution = registry.execute(
        "update_stock_data",
        '{"symbols": ["600519"], "start_date": "2026-01-01", "end_date": "2026-02-01"}',
    )
    assert execution.ok is True
    assert captured["symbols"] == ["600519"]
    assert captured["cache"] is backend.cache
    assert captured["warehouse"] is backend.warehouse
    assert callable(captured["fetcher"])
    assert callable(captured["capital_flow_fetcher"])
    assert "写入 120 行" in execution.summary
    assert any(level == "info" for level, _ in backend.logged)

    too_many_symbols = "[" + ",".join(f'"{index:06d}"' for index in range(21)) + "]"
    rejected = registry.execute(
        "update_stock_data",
        '{"symbols": ' + too_many_symbols + ', "start_date": "2026-01-01", "end_date": "2026-02-01"}',
    )
    assert rejected.ok is False and "20" in rejected.summary

    bad_range = registry.execute(
        "update_stock_data",
        '{"symbols": ["600519"], "start_date": "2020-01-01", "end_date": "2026-02-01"}',
    )
    assert bad_range.ok is False and "400" in bad_range.summary
