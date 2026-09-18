from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pandas as pd
from astock_backtester.ai.tools import astock_data_tools
from astock_backtester.ai.tools.astock_data_tools import build_astock_data_tools
from astock_backtester.ai.tools.local_tools import build_local_tools
from astock_backtester.ai.tools.registry import ToolRegistry
from astock_backtester.models import (
    MarketBreadth,
    MarketIndexQuote,
    MarketNewsItem,
    MarketNewsResponse,
    RealtimeMarketSnapshot,
    RiskAlertItem,
    RiskAlertsResponse,
)


class FakeWarehouse:
    def __init__(self, owner: FakeBackend) -> None:
        self._owner = owner
        self.parquet_paths: list[str] = []
        self.gap_profile: dict[str, Any] = {"available": False, "reason": "fake warehouse 无数据"}

    def read_daily_bars(self, **kwargs: Any) -> pd.DataFrame:
        return self._owner.read_daily_bars(**kwargs)

    def daily_bars_parquet_paths(self) -> list[str]:
        return self.parquet_paths

    def data_gap_profile(self, **kwargs: Any) -> dict[str, Any]:
        return self.gap_profile


class FakeBackend:
    def __init__(self) -> None:
        self.read_calls: list[dict[str, Any]] = []
        self.frame = pd.DataFrame()
        self.cache = SimpleNamespace(root="cache-root")
        self.provider = SimpleNamespace(fetch_daily_bars=lambda symbol, start, end: pd.DataFrame())
        self.capital_flow_crawler = SimpleNamespace(
            fetch_many_fund_flows=lambda symbols, start, end, timeout=15: {"rows": []}
        )
        self.warehouse = FakeWarehouse(self)
        self.logged: list[tuple[str, str]] = []

    def read_daily_bars(self, **kwargs: Any) -> pd.DataFrame:
        self.read_calls.append(kwargs)
        return self.frame

    realtime_provider = SimpleNamespace(market_snapshot=lambda: _snapshot())
    news_provider = SimpleNamespace(latest_news=lambda: _news())
    briefing_provider = SimpleNamespace(
        latest_fupan=lambda: SimpleNamespace(
            model_dump=lambda mode="json": {"source": "ths-fupan", "summary": "复盘要点", "sections": [{"title": "大盘"}]}
        )
    )
    risk_provider = SimpleNamespace(current_alerts=lambda: _risk())

    def log(self, level: str, message: str) -> None:
        self.logged.append((level, message))


def _snapshot() -> RealtimeMarketSnapshot:
    return RealtimeMarketSnapshot(
        status="live",
        source="fake",
        updated_at=datetime.now(UTC),
        indexes=[MarketIndexQuote(symbol="sh000001", name="上证指数", last=3100.0, change_pct=0.65, source="fake")],
        breadth=MarketBreadth(up=3200, down=1800, flat=120, total=5120, source="fake"),
        strong_sectors=[],
        message="ok",
    )


def _news() -> MarketNewsResponse:
    return MarketNewsResponse(
        updated_at=datetime.now(UTC),
        source="fake",
        items=[MarketNewsItem(title="央行发布新政策", source="财联社", published_at=datetime.now(UTC))],
    )


def _risk() -> RiskAlertsResponse:
    return RiskAlertsResponse(
        updated_at=datetime.now(UTC),
        source="fake",
        items=[
            RiskAlertItem(
                symbol="600000",
                name="测试风险",
                risk_type="st",
                reason="测试",
                severity="high",
                source="fake",
                detected_at=datetime.now(UTC),
            )
        ],
    )


def _bars_frame() -> pd.DataFrame:
    rows = []
    for index in range(30):
        rows.append(
            {
                "symbol": "600519",
                "stock_name": "贵州茅台",
                "trade_date": f"2026-01-{index + 1:02d}",
                "open": 100.0 + index,
                "high": 101.0 + index,
                "low": 99.0 + index,
                "close": 100.5 + index,
                "volume": 1000 + index,
                "amount": 100.0 + index,
                "change_pct": 0.001,
                "turnover_rate": 0.02,
                "volume_ratio": 1.0,
                "float_market_cap": 1e10,
                "total_market_cap": 2e10,
                "main_net_inflow": 1e6,
                "is_st": False,
                "is_suspended": False,
                "source": "test",
            }
        )
    return pd.DataFrame(rows)


def _tool(registry: ToolRegistry, name: str):
    tool = registry.get(name)
    assert tool is not None
    return tool


def test_local_tools_registered_with_schemas():
    registry = ToolRegistry()
    registry.register_all(build_local_tools(FakeBackend()))
    names = registry.names()
    assert {
        "realtime_market_snapshot",
        "market_news",
        "market_briefing",
        "risk_alerts",
        "recent_daily_bars",
        "validate_strategy_conditions",
        "run_strategy_backtest",
        "data_health_report",
    }.issubset(set(names))
    for schema in registry.openai_schemas():
        assert schema["function"]["parameters"]["type"] == "object"


def test_data_health_report_tool_surfaces_gap_details():
    backend = FakeBackend()
    backend.warehouse.gap_profile = {
        "available": True,
        "window": {"start_date": "2026-06-01", "end_date": "2026-06-04", "partitions": ["year=2026"]},
        "daily_bars": {
            "symbols": 5463,
            "symbols_current": 74,
            "symbols_stale": 5389,
            "stale_distribution": [
                {"last_date": "2026-07-14", "symbols": 3084},
                {"last_date": "2025-12-31", "symbols": 1080},
            ],
            "thin_days": [{"trade_date": "2026-09-09", "rows": 74}],
        },
        "market_cap": {"symbols": 5463, "stale_distribution": [{"last_date": "2026-09-04", "symbols": 1222}]},
        "capital_flow": {"symbols": 5463, "stale_distribution": []},
    }
    registry = ToolRegistry()
    registry.register_all(build_local_tools(backend))
    execution = registry.execute("data_health_report", "{}")
    assert execution.ok is True
    # 摘要把“具体缺哪些”讲清楚：停更分布、写入失败日、字段尾部
    assert "3084 只停在 2026-07-14" in execution.summary
    assert "2026-09-09" in execution.summary
    assert "市值停更" in execution.summary
    assert execution.payload["profile"]["daily_bars"]["symbols_stale"] == 5389


def test_data_health_report_tool_handles_empty_warehouse():
    backend = FakeBackend()
    backend.warehouse.gap_profile = {"available": False, "reason": "本地数据仓还没有日线分区。"}
    registry = ToolRegistry()
    registry.register_all(build_local_tools(backend))
    execution = registry.execute("data_health_report", "{}")
    assert execution.ok is False
    assert "还没有日线分区" in execution.summary


def test_realtime_and_news_tool_summaries():
    registry = ToolRegistry()
    registry.register_all(build_local_tools(FakeBackend()))
    snapshot = registry.execute("realtime_market_snapshot", "{}")
    assert snapshot.ok is True
    assert "上证指数" in snapshot.summary and "红盘 3200" in snapshot.summary
    news = registry.execute("market_news", '{"limit": 5}')
    assert news.ok is True
    assert "央行发布新政策" in news.summary


def test_recent_daily_bars_returns_indicators():
    backend = FakeBackend()
    backend.frame = _bars_frame()
    registry = ToolRegistry()
    registry.register_all(build_local_tools(backend))
    execution = registry.execute("recent_daily_bars", '{"symbol": "SH600519", "days": 10}')
    assert execution.ok is True
    payload = execution.payload
    assert payload["symbol"] == "600519"
    assert payload["rows"][-1]["ma20"] is not None
    assert "贵州茅台" in execution.summary


def test_recent_daily_bars_missing_data_fails_gracefully():
    registry = ToolRegistry()
    registry.register_all(build_local_tools(FakeBackend()))
    execution = registry.execute("recent_daily_bars", '{"symbol": "600519"}')
    assert execution.ok is False
    assert "补齐" in execution.summary


def test_validate_strategy_conditions_feedback_loop():
    registry = ToolRegistry()
    registry.register_all(build_local_tools(FakeBackend()))
    ok = registry.execute(
        "validate_strategy_conditions",
        '{"entry_expressions": ["收盘价站上20日均线"], "exit_expressions": ["MACD死叉"]}',
    )
    assert ok.ok is True
    bad = registry.execute("validate_strategy_conditions", '{"entry_expressions": ["布林带突破上轨"]}')
    assert bad.ok is False
    failure = bad.payload["failures"][0]
    assert failure["errors"] and failure["examples"]


def test_run_strategy_backtest_enforces_limits():
    registry = ToolRegistry()
    registry.register_all(build_local_tools(FakeBackend()))
    too_long = registry.execute(
        "run_strategy_backtest",
        '{"entry_expressions": ["收盘价站上20日均线"], "start_date": "2020-01-01", "end_date": "2026-01-01"}',
    )
    assert too_long.ok is False and "366" in too_long.summary
    empty_pool = registry.execute(
        "run_strategy_backtest",
        '{"entry_expressions": ["收盘价站上20日均线"], "start_date": "2026-01-01", "end_date": "2026-02-01", "stock_pool": "custom"}',
    )
    assert empty_pool.ok is False
    invalid_condition = registry.execute(
        "run_strategy_backtest",
        '{"entry_expressions": ["KDJ金叉"], "start_date": "2026-01-01", "end_date": "2026-02-01"}',
    )
    assert invalid_condition.ok is False and "failures" in invalid_condition.payload


def test_stock_valuation_parses_tencent_payload(monkeypatch):
    fields = ["0"] * 53
    fields[1] = "贵州茅台"
    fields[3] = "1523.40"
    fields[4] = "1530.00"
    fields[32] = "0.43"
    fields[37] = "50000"
    fields[38] = "0.31"
    fields[39] = "22.50"
    fields[44] = "19100.00"
    fields[45] = "19200.00"
    fields[46] = "8.30"
    fields[49] = "1.20"
    fields[52] = "23.80"
    line = 'v_sh600519="' + "~".join(fields) + '"'

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        content = line.encode("gbk")

    class FakeSession:
        def get(self, url, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(astock_data_tools, "create_scraping_session", lambda: FakeSession())
    registry = ToolRegistry()
    registry.register_all(build_astock_data_tools())
    execution = registry.execute("stock_valuation", '{"symbols": ["600519"]}')
    assert execution.ok is True
    quote = execution.payload["quotes"][0]
    assert quote["symbol"] == "600519"
    assert quote["name"] == "贵州茅台"
    assert quote["price"] == 1523.4
    assert quote["pe_ttm"] == 22.5
    assert quote["pb"] == 8.3
    assert quote["total_mcap_yi"] == 19200.0
    assert quote["is_stale"] is False
    assert "贵州茅台" in execution.summary


def test_research_reports_and_pools_handle_failure(monkeypatch):
    def failing_em_get(url, **kwargs):
        raise RuntimeError("blocked")

    monkeypatch.setattr(astock_data_tools, "_em_get", failing_em_get)
    registry = ToolRegistry()
    registry.register_all(build_astock_data_tools())
    reports = registry.execute("stock_research_reports", '{"symbol": "600519"}')
    assert reports.ok is False and "失败" in reports.summary
    pool = registry.execute("limit_up_pool", '{"pool_type": "zt"}')
    assert pool.ok is False


def test_limit_up_pool_parses_rows(monkeypatch):
    captured = {}

    def fake_em_get(url, *, params=None, headers=None, timeout=12):
        captured["url"] = url
        captured["params"] = params
        row = {"c": "600519", "n": "贵州茅台", "p": 1523000, "zdp": 10.0, "lbc": 2, "zbc": 1, "hybk": "白酒", "zttj": {"days": 3, "ct": 2}}
        return SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"data": {"pool": [row]}}
        )

    monkeypatch.setattr(astock_data_tools, "_em_get", fake_em_get)
    registry = ToolRegistry()
    registry.register_all(build_astock_data_tools())
    execution = registry.execute("limit_up_pool", '{"pool_type": "zt", "trade_date": "20260910"}')
    assert execution.ok is True
    assert captured["params"]["date"] == "20260910"
    item = execution.payload["items"][0]
    assert item["price"] == 1523.0 and item["zt_stat"] == "3天2板"
    assert "连板2" in execution.summary


def test_read_tool_result_pages_a_stored_rowset():
    """摘要写着"另有 M 行未展开"，模型就必须能按行把剩下的取回来。"""
    from astock_backtester.ai.context import ToolResult, ToolResultStore
    from astock_backtester.ai.tools.registry import build_read_result_tool

    store = ToolResultStore()
    rows = [{"symbol": f"{index:06d}", "close": 10.0 + index} for index in range(50)]
    store.put(
        ToolResult(
            call_id="c1",
            name="query_warehouse_sql",
            arguments={},
            payload={"ok": True, "rows": rows},
            summary="ignored",
        )
    )
    registry = ToolRegistry()
    registry.register(build_read_result_tool(store))

    first = registry.execute("read_tool_result", '{"call_id": "c1"}')
    assert first.ok is True
    assert first.payload["shown_rows"] == 40 and first.payload["more_rows"] == 10
    assert "第 1~40 行（共 50 行）" in first.summary
    assert "还剩 10 行未读，可继续 offset=40" in first.summary

    second = registry.execute("read_tool_result", '{"call_id": "c1", "offset": 40}')
    assert second.payload["shown_rows"] == 10 and second.payload["more_rows"] == 0
    assert "该结果已全部读完" in second.summary

    gone = registry.execute("read_tool_result", '{"call_id": "nope"}')
    assert gone.ok is False and gone.code == "result_evicted"
    assert "不是数据不存在" in gone.summary
