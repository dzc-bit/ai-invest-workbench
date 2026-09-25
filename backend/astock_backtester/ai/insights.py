"""Timely-events layer: rule triggers + throttled AI-generated insights.

``EventBroker`` fans NDJSON events out to /ai/events/stream subscribers:

- ``data_fresh``: cheap backend signals ("this module has new data") that let
  the frontend refresh immediately instead of waiting for its polling cycle.
  They need no LLM and are emitted even when AI is unconfigured.
- ``insight``: AI-generated short briefs, produced only when a model is
  configured, behind an hourly cap (cost guard), and always labelled as
  AI-generated.  Insights never replace or masquerade as market data modules.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from astock_backtester.ai.config import AiConfig
from astock_backtester.ai.llm_client import ChatModel
from astock_backtester.ai.models import AiInsightRecord
from astock_backtester.ai.prompts import build_insight_messages

HEARTBEAT_INTERVAL_SECONDS = 15.0
DEFAULT_ENGINE_INTERVAL_SECONDS = 60.0
# 同一触发点（同标题/同一状态）的快讯冷却窗口：市场宽度持续极端时，
# 引擎每分钟 tick 一次，若不去重会连发多条内容雷同的快讯。
INSIGHT_DEDUP_WINDOW_SECONDS = 2 * 3600.0


class EventBroker:
    def __init__(self, max_queue: int = 200) -> None:
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []
        self._lock = threading.Lock()
        self._max_queue = max_queue

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        stream: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self._max_queue)
        with self._lock:
            self._subscribers.append(stream)
        return stream

    def unsubscribe(self, stream: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            if stream in self._subscribers:
                self._subscribers.remove(stream)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for stream in subscribers:
            try:
                stream.put_nowait(event)
            except queue.Full:
                pass

    def has_subscribers(self) -> bool:
        with self._lock:
            return bool(self._subscribers)


class InsightEngine:
    """Background rule engine; ``tick()`` is synchronous for testability."""

    def __init__(
        self,
        broker: EventBroker,
        backend: Any,
        model_provider: Callable[[], ChatModel | None],
        config_provider: Callable[[], AiConfig],
        *,
        interval_seconds: float = DEFAULT_ENGINE_INTERVAL_SECONDS,
    ) -> None:
        self._broker = broker
        self._backend = backend
        self._model_provider = model_provider
        self._config_provider = config_provider
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_news_digest = ""
        self._last_breadth_ratio: float | None = None
        self._last_risk_count: int | None = None
        self._insight_times: deque[float] = deque()
        # dedup_key -> 上次发布时间（monotonic）；用于同因快讯冷却。
        self._insight_signatures: dict[str, float] = {}

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ai-insight-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - the engine must never crash the service
                try:
                    self._backend.log("warning", f"ai insight engine tick failed: {exc}")
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------ tick
    def tick(self) -> None:
        if not self._broker.has_subscribers():
            return
        self._check_news()
        self._check_market()
        self._check_risk()

    def _check_news(self) -> None:
        try:
            response = self._backend.news_provider.latest_news()
        except Exception:  # noqa: BLE001
            return
        digest = "|".join(item.title for item in response.items[:5])
        if self._last_news_digest and digest != self._last_news_digest:
            self._broker.publish({"type": "data_fresh", "module": "news", "timestamp": datetime.now(UTC).isoformat()})
        self._last_news_digest = digest

    def _check_market(self) -> None:
        try:
            snapshot = self._backend.realtime_provider.market_snapshot()
        except Exception:  # noqa: BLE001
            return
        breadth = snapshot.breadth
        if breadth is None or breadth.total <= 0:
            return
        ratio = breadth.up / breadth.total
        if self._last_breadth_ratio is not None and abs(ratio - self._last_breadth_ratio) > 0.03:
            self._broker.publish({"type": "data_fresh", "module": "market", "timestamp": datetime.now(UTC).isoformat()})
        self._last_breadth_ratio = ratio
        extreme = ratio < 0.25 or ratio > 0.75
        if extreme:
            # 去重键按 5 个百分点分桶：宽度在同一区间内反复震荡时不再连发。
            bucket = int(round(ratio * 20))
            self._maybe_generate_insight(
                level="warning",
                title=f"市场宽度异常：红盘占比 {ratio:.0%}",
                data=f"红盘 {breadth.up} / 全市场 {breadth.total}（占比 {ratio:.0%}），来源 {breadth.source}，状态 {snapshot.status}",
                dedup_key=f"breadth-extreme:{'high' if ratio > 0.5 else 'low'}:{bucket}",
            )

    def _check_risk(self) -> None:
        try:
            response = self._backend.risk_provider.current_alerts()
        except Exception:  # noqa: BLE001
            return
        count = len(response.items)
        if self._last_risk_count is not None and count > self._last_risk_count:
            self._broker.publish({"type": "data_fresh", "module": "risk", "timestamp": datetime.now(UTC).isoformat()})
        self._last_risk_count = count

    # -------------------------------------------------------------- insights
    def _insight_cap_reached(self, config: AiConfig) -> bool:
        now = time.monotonic()
        while self._insight_times and now - self._insight_times[0] > 3600:
            self._insight_times.popleft()
        return len(self._insight_times) >= config.insight_max_per_hour

    def _maybe_generate_insight(self, *, level: str, title: str, data: str, dedup_key: str | None = None) -> None:
        config = self._config_provider()
        if not config.is_configured() or not config.insights_enabled or config.insight_max_per_hour <= 0:
            return
        if self._insight_cap_reached(config):
            return
        now = time.monotonic()
        if dedup_key:
            self._prune_insight_signatures(now)
            last = self._insight_signatures.get(dedup_key)
            if last is not None and now - last < INSIGHT_DEDUP_WINDOW_SECONDS:
                return
        model = self._model_provider()
        if model is None:
            return
        content = ""
        # 快讯面向用户播报，允许带轻量人设语气；聚合要点（DIGEST_PROMPT）保持
        # 中立——口径登记在 prompts.STYLE_FREE_PROMPTS，两者不是一个出口。
        for event in model.chat(build_insight_messages(data, style=config.research_style), tools=None):
            if event[0] == "final":
                content = str(event[1].get("content") or "")
        content = content.strip()
        if not content or content == "NO_INSIGHT":
            return
        self._insight_times.append(time.monotonic())
        if dedup_key:
            self._insight_signatures[dedup_key] = time.monotonic()
        self._broker.publish(
            {
                "type": "insight",
                "insight": AiInsightRecord(
                    id=uuid4().hex,
                    created_at=datetime.now(UTC),
                    level=level,  # type: ignore[arg-type]
                    title=title,
                    digest=content[:600],
                ).model_dump(mode="json"),
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

    def _prune_insight_signatures(self, now: float) -> None:
        expired = [key for key, at in self._insight_signatures.items() if now - at >= INSIGHT_DEDUP_WINDOW_SECONDS]
        for key in expired:
            self._insight_signatures.pop(key, None)
