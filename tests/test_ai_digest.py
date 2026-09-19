from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from astock_backtester.ai.config import AiConfig
from astock_backtester.ai.digest import DigestEngine, DigestStore, parse_digest_items
from astock_backtester.ai.insights import EventBroker
from astock_backtester.models import (
    MarketBreadth,
    MarketIndexQuote,
    MarketNewsItem,
    MarketNewsResponse,
    RealtimeMarketSnapshot,
)


@pytest.fixture(autouse=True)
def _no_external_crawl(monkeypatch):
    """简报引擎的涨停池来自东财公开 XHR；单测不打真实网络。

    否则离线/CI 环境里每次 run_once 多出秒级 DNS/连接超时，
    ``_gather_sources`` 变慢、线程循环的计时断言随机翻车。
    """
    monkeypatch.setattr(
        "astock_backtester.ai.tools.astock_data_tools.fetch_limit_up_rows",
        lambda kind: [],
    )

DIGEST_JSON = (
    '[{"title": "光量子计算取得突破", "summary": "图灵量子发布第三代光量子计算机。", '
    '"tags": ["行业"], "symbols": ["688047"]},'
    '{"title": "市场宽度回暖", "summary": "红盘占比回升至六成。", "tags": ["情绪"], "symbols": []}]'
)


class ScriptedModel:
    def __init__(self, content: str) -> None:
        self.content = content
        self.prompts: list[list[dict[str, Any]]] = []

    def chat(self, messages: list[dict[str, Any]], *, tools: Any = None):
        self.prompts.append(messages)
        yield ("final", {"content": self.content, "tool_calls": None})

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0]]


class FakeBackend:
    def __init__(self) -> None:
        self.logged: list[str] = []
        self.news_titles = ["图灵量子首发光量子计算机", "习近平会见印度总理莫迪"]
        self.up = 3200
        self.total = 5120

    news_provider = SimpleNamespace()
    realtime_provider = SimpleNamespace()
    briefing_provider = SimpleNamespace()

    def latest_news(self) -> MarketNewsResponse:
        return MarketNewsResponse(
            updated_at=datetime.now(UTC),
            source="fake",
            items=[MarketNewsItem(title=title, source="财联社电报") for title in self.news_titles],
        )

    def snapshot(self) -> RealtimeMarketSnapshot:
        return RealtimeMarketSnapshot(
            status="live",
            source="fake",
            updated_at=datetime.now(UTC),
            indexes=[MarketIndexQuote(symbol="sh000001", name="上证指数", last=3100.0, change_pct=0.5, source="fake")],
            breadth=MarketBreadth(up=self.up, down=self.total - self.up, flat=0, total=self.total, source="fake"),
            message="ok",
        )

    def log(self, level: str, message: str) -> None:
        self.logged.append(message)


def _wire(backend: FakeBackend) -> None:
    backend.news_provider = SimpleNamespace(latest_news=backend.latest_news)
    backend.realtime_provider = SimpleNamespace(market_snapshot=backend.snapshot)
    backend.briefing_provider = SimpleNamespace(
        latest_fupan=lambda: SimpleNamespace(summary="复盘要点文字"),
        latest_zaopan=lambda: SimpleNamespace(summary=""),
    )


def _engine(tmp_path, backend: FakeBackend, model_content: str = DIGEST_JSON):
    _wire(backend)
    broker = EventBroker()
    broker.subscribe()  # 有订阅者时 tick/run 才会发布
    engine = DigestEngine(
        broker=broker,
        backend=backend,
        model_provider=lambda: ScriptedModel(model_content),
        config_provider=lambda: AiConfig(base_url="http://x", api_key="k", model="m"),
        store=DigestStore(tmp_path),
    )
    return engine, broker


def test_parse_digest_items_bounds_and_defaults():
    items = parse_digest_items(DIGEST_JSON)
    assert len(items) == 2
    assert items[0].source == "ai-agent"
    assert items[0].tags == ["行业"]
    assert items[1].symbols == []
    assert parse_digest_items("不是 json") == []


def test_run_once_gathers_stores_and_publishes(tmp_path):
    engine, broker = _engine(tmp_path, FakeBackend())
    result = engine.run_once()
    assert result["ok"] is True and result["items"] == 2

    view = engine.view()
    assert view["count"] == 2
    assert view["items"][0]["title"].startswith(("光量子", "市场宽度"))

    # 去重：同一标题不重复入库
    engine.run_once(force=True)
    assert engine.view()["count"] == 2


def test_run_once_skips_when_unconfigured_or_recent(tmp_path):
    backend = FakeBackend()
    _wire(backend)
    engine = DigestEngine(
        broker=EventBroker(),
        backend=backend,
        model_provider=lambda: ScriptedModel(DIGEST_JSON),
        config_provider=lambda: AiConfig(),
        store=DigestStore(tmp_path),
    )
    assert engine.run_once()["skipped"] == "not_configured"

    configured_engine, _ = _engine(tmp_path / "b", backend)
    assert configured_engine.run_once()["ok"] is True
    assert configured_engine.run_once()["skipped"] == "recent_run"


def test_gather_sources_combines_news_market_and_briefing(tmp_path):
    engine, _ = _engine(tmp_path, FakeBackend())
    data_text = engine._gather_sources()
    assert "【新闻/电报】" in data_text
    assert "财联社电报" in data_text
    assert "【实时行情】" in data_text
    assert "上证指数" in data_text
    assert "【同花顺复盘】" in data_text


def test_fresh_engine_never_treated_as_recent_run_on_low_uptime_machines(tmp_path, monkeypatch):
    """time.monotonic() counts from an arbitrary point (boot on Windows); a
    freshly provisioned CI runner can report < FRESH_THRESHOLD_SECONDS. A new
    engine must therefore never be skipped as ``recent_run`` regardless of the
    current monotonic reading — it must fall through to the config check."""
    from astock_backtester.ai import digest as digest_module
    from astock_backtester.ai.insights import EventBroker

    monkeypatch.setattr(digest_module.time, "monotonic", lambda: 120.0)  # 模拟刚开机的机器
    backend = FakeBackend()
    _wire(backend)
    engine = DigestEngine(
        broker=EventBroker(),
        backend=backend,
        model_provider=lambda: ScriptedModel(DIGEST_JSON),
        config_provider=lambda: AiConfig(),
        store=DigestStore(tmp_path),
    )

    assert engine.run_once()["skipped"] == "not_configured"


def test_loop_survives_upstream_exception_and_logs_it(tmp_path):
    """一次上游异常不得杀死简报线程。

    线程一死，3 小时定时器随之消失，资讯与事件面板会静默停摆到下次重启，
    并且不留任何日志痕迹——这正是 run_once 抛出 AiUpstreamError 时的表现。
    """
    backend = FakeBackend()
    _wire(backend)

    class RaisingModel:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages: list[dict[str, Any]], *, tools: Any = None):
            # 真实客户端是生成器，异常发生在迭代时，这里保持一致
            self.calls += 1
            raise RuntimeError("ai_upstream_error: 503 upstream reset")
            yield  # pragma: no cover

    model = RaisingModel()
    engine = DigestEngine(
        broker=EventBroker(),
        backend=backend,
        model_provider=lambda: model,
        config_provider=lambda: AiConfig(base_url="http://x", api_key="k", model="m"),
        store=DigestStore(tmp_path),
        interval_seconds=0.05,
    )
    engine.start()
    try:
        deadline = time.monotonic() + 5
        while model.calls < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        engine.stop()

    assert model.calls >= 3, f"简报线程在异常后停跑了（只尝试 {model.calls} 次）"
    assert any("ai digest engine run failed" in entry for entry in backend.logged), backend.logged
