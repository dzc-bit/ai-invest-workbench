"""AiService facade: the single object the HTTP service talks to.

Wires config store, LLM client, tool registry, agent loop, sessions, knowledge
index and the insight engine together.  Constructed lazily by
``DataServiceState`` so a user who never touches AI features pays nothing.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from astock_backtester.ai.agent import AgentRunner
from astock_backtester.ai.condition_dsl import parse_conditions_with_llm
from astock_backtester.ai.config import AiConfig, AiConfigStore, ai_base_dir_from_cache_dir
from astock_backtester.ai.context import ContextBudget, ToolResultStore
from astock_backtester.ai.digest import DigestEngine, DigestStore
from astock_backtester.ai.errors import AiError, AiNotConfigured, AiSessionBusy, AiSessionNotFound, ai_error_code
from astock_backtester.ai.insights import HEARTBEAT_INTERVAL_SECONDS, EventBroker, InsightEngine
from astock_backtester.ai.llm_client import OpenAiCompatibleClient
from astock_backtester.ai.memory import MemoryStore, plan_memory_ops
from astock_backtester.ai.models import AiChatRequest, AiStatusResponse
from astock_backtester.ai.oneshot import ONESHOT_SCENES, insight_oneshot
from astock_backtester.ai.overfit import assess_overfit
from astock_backtester.ai.prompts import build_system_prompt
from astock_backtester.ai.rag.retriever import KnowledgeIndex, build_knowledge_tool
from astock_backtester.ai.reports import ReportStore, ScheduledReportEngine
from astock_backtester.ai.sessions import SessionStore, sanitize_session_id
from astock_backtester.ai.tools.astock_data_tools import build_astock_data_tools
from astock_backtester.ai.tools.local_tools import build_local_tools
from astock_backtester.ai.tools.query_tools import build_query_tools
from astock_backtester.ai.tools.registry import ToolRegistry, build_read_result_tool

# 同一会话上一轮仍在生成时，新一轮最多等待多久（用户点“停止”后 worker 仍在收尾）。
AI_SESSION_LOCK_TIMEOUT_SECONDS = 90.0
# 事件流静默多久就发一个 heartbeat 保活（前端空闲超时是 180 秒，留足余量）。
AI_STREAM_HEARTBEAT_SECONDS = 15.0
# 会话锁字典上限：超过后淘汰未被持有的锁，避免长跑进程内存只增不减。
AI_MAX_SESSION_LOCKS = 512

# 中国无夏令时，固定 +08:00 即可；不用 zoneinfo 是因为 Windows 上它依赖 tzdata 包。
BEIJING_TZ = timezone(timedelta(hours=8))
_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def today_context(now: datetime | None = None) -> str:
    """模型不看系统时钟：不给当天日期，"今天/近期"只能靠猜，猜错就查空再编数。"""
    local = now or datetime.now(BEIJING_TZ)
    return (
        f"## 当前时间\n今天是 {local:%Y-%m-%d}（{_WEEKDAY_NAMES[local.weekday()]}，北京时间）。"
        "“今天/昨天/近期”一律以此为基准；本地数据仓的最新交易日必须用工具确认"
        "（realtime_market_snapshot 或 query_warehouse_sql），不要凭日期推断行情。"
    )


class _SessionLockEntry:
    """会话锁 + 引用计数。

    引用计数存在的唯一理由是让淘汰变得安全：旧实现用 ``lock.locked()`` 判断
    “空闲”，但“已从字典取出、还没 acquire”的窗口里锁同样是空闲的，淘汰线程
    可以在此时把条目删掉 —— 于是同一会话先后出现两把互不相斥的锁，会话互斥
    静默失效。现在任何持有者都必须先 ``retain()``，引用计数 > 0 的条目永不
    淘汰；调用方在 ``finally`` 里 ``release()`` 时一并归还引用。
    """

    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.refs = 0

    def acquire(self, timeout: float | None = None) -> bool:
        if timeout is None:
            return self.lock.acquire()
        return self.lock.acquire(timeout=timeout)

    def release(self) -> None:
        self.lock.release()


class AiService:
    def __init__(self, *, cache_dir: str | Path, backend: Any, log: Any) -> None:
        base_dir = ai_base_dir_from_cache_dir(cache_dir)
        self._config_store = AiConfigStore(base_dir)
        self._sessions = SessionStore(base_dir)
        self._model = OpenAiCompatibleClient(self._config_store.load)
        self._result_store = ToolResultStore()
        self._budget = ContextBudget()
        self._registry = ToolRegistry()
        self._registry.register_all(build_local_tools(backend))
        self._registry.register_all(build_astock_data_tools(backend))
        self._registry.register_all(build_query_tools(backend))
        self._registry.register(build_read_result_tool(self._result_store))
        self._knowledge = KnowledgeIndex(
            embedder=self._model.embed,
            cache_dir=base_dir / "AI缓存",
        )
        self._registry.register(build_knowledge_tool(self._knowledge, self._budget))
        self._agent = AgentRunner(self._model, self._registry, self._result_store, self._budget)
        self._memory = MemoryStore(base_dir)
        self._digest_store = DigestStore(base_dir)
        self._broker = EventBroker()
        self._digest = DigestEngine(
            broker=self._broker,
            backend=backend,
            model_provider=lambda: self._model if self._config_store.load().is_configured() else None,
            config_provider=self._config_store.load,
            store=self._digest_store,
        )
        self._registry.register(self._build_digest_tool())
        self._engine = InsightEngine(
            self._broker,
            backend,
            model_provider=lambda: self._model if self._config_store.load().is_configured() else None,
            config_provider=self._config_store.load,
        )
        self._engine.start()
        self._digest.start()
        self._report_store = ReportStore(base_dir)
        self._reports = ScheduledReportEngine(
            backend=backend,
            model_provider=lambda: self._model if self._config_store.load().is_configured() else None,
            config_provider=self._config_store.load,
            store=self._report_store,
            digest_items_provider=lambda: [
                {"title": item.title, "summary": item.summary} for item in self._digest_store.load()
            ],
            ai_base_dir=base_dir,
        )
        self._reports.start()
        self._session_locks: dict[str, _SessionLockEntry] = {}
        self._session_locks_guard = threading.Lock()
        self._log = log
        self._log("info", f"AI 子系统已初始化：{len(self._registry.names())} 个工具（未配置模型前仅提供状态与快讯通道）")

    # ---------------------------------------------------------------- status
    def status(self) -> AiStatusResponse:
        config = self._config_store.load()
        knowledge_info = self._knowledge.info()
        return AiStatusResponse(
            configured=config.is_configured(),
            base_url=config.base_url,
            model=config.model,
            insights_enabled=config.insights_enabled and config.is_configured(),
            tool_names=self._registry.names(),
            knowledge_documents=knowledge_info["documents"],
            knowledge_chunks=knowledge_info["chunks"],
            knowledge_ready=knowledge_info["ready"],
            memory_count=self._memory.count(),
        )

    def news_digest_view(self) -> dict[str, Any]:
        return self._digest.view()

    def refresh_news_digest(self) -> dict[str, Any]:
        return self._digest.run_once(force=True)

    def _build_digest_tool(self) -> Any:
        from astock_backtester.ai.tools.registry import AiTool

        store = self._digest_store

        def execute(_: dict[str, Any]) -> dict[str, Any]:
            items = store.load()
            if not items:
                return {"ok": False, "error": "还没有 AI 聚合简报（启动且配置模型后自动生成）。"}
            rows = [
                {key: getattr(item, key) for key in ("title", "summary", "tags", "symbols", "created_at")}
                for item in items[:8]
            ]
            return {"ok": True, "items": rows}

        def summarize(payload: dict[str, Any]) -> str:
            lines = ["AI 聚合要点："]
            for item in payload.get("items", []):
                lines.append(f"- {item.get('title')}｜{item.get('summary')}")
            return "\n".join(lines)

        return AiTool(
            name="latest_market_digest",
            description="读取启动时 AI 自动聚合的多源市场要点（新闻/涨停池/行情/复盘），回答'今日发生了什么/最新消息'前先调用。",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            executor=execute,
            summarizer=summarize,
        )

    def reveal_api_key(self) -> str:
        return self._config_store.load().api_key

    def config_view(self) -> dict[str, Any]:
        return self._config_store.masked_view()

    def save_config(self, payload: dict[str, Any]) -> dict[str, Any]:

        current = self._config_store.load()
        merged = AiConfig(
            base_url=str(payload.get("base_url", current.base_url)),
            api_key=str(payload.get("api_key", "") or ""),
            model=str(payload.get("model", current.model)),
            embedding_model=str(payload.get("embedding_model", current.embedding_model)),
            embedding_base_url=str(payload.get("embedding_base_url", "") or "") or current.embedding_base_url,
            embedding_api_key=str(payload.get("embedding_api_key", "") or ""),
            api_style=str(payload.get("api_style", current.api_style)),
            research_style=str(payload.get("research_style", current.research_style)),
            temperature=float(payload.get("temperature", current.temperature)),
            max_tokens=int(payload.get("max_tokens", current.max_tokens)),
            max_steps=int(payload.get("max_steps", current.max_steps)),
            insights_enabled=bool(payload.get("insights_enabled", current.insights_enabled)),
            insight_max_per_hour=int(payload.get("insight_max_per_hour", current.insight_max_per_hour)),
            report_enabled=bool(payload.get("report_enabled", current.report_enabled)),
            report_time=str(payload.get("report_time", current.report_time)),
            evolution_enabled=bool(payload.get("evolution_enabled", current.evolution_enabled)),
            evolution_time=str(payload.get("evolution_time", current.evolution_time)),
        )
        saved = self._config_store.save(merged)
        return {"ok": True, "configured": saved.is_configured(), **self._config_store.masked_view()}

    # ------------------------------------------------------------ one-shot AI
    def _require_model(self) -> Any:
        config = self._config_store.load()
        if not config.is_configured():
            raise AiNotConfigured("AI 服务尚未配置，请先在设置中填写 base_url、API Key 和模型名。")
        return self._model

    def parse_conditions(self, text: str) -> dict[str, Any]:
        """Natural-language rules → validated entry/exit DSL (self-healing)."""
        model = self._require_model()
        return parse_conditions_with_llm(model, text)

    def insight_oneshot(self, scene: str, context: Any) -> dict[str, Any]:
        """Single-paragraph AI commentary for a named UI scene."""
        if scene not in ONESHOT_SCENES:
            raise ValueError(f"未知点评场景：{scene}（可选：{', '.join(ONESHOT_SCENES)}）")
        model = self._require_model()
        text = insight_oneshot(model, scene, context)
        return {"ok": True, "scene": scene, "text": text, "generated_at": datetime.now(UTC).isoformat()}

    # ------------------------------------------------------------------ chat
    def _session_lock(self, session_id: str) -> _SessionLockEntry:
        """取回（必要时创建）会话锁，并把引用计数 +1。

        调用方必须在 ``finally`` 里 ``entry.release()`` —— 它同时归还引用计数
        并释放锁，两者不会漏。
        """
        with self._session_locks_guard:
            entry = self._session_locks.get(session_id)
            if entry is None:
                # 长跑 sidecar 里会话数只增不减，无上限的锁字典是缓慢的内存泄漏；
                # 超过上限时淘汰引用计数为 0 的条目（>0 说明仍有调用方持有或
                # 即将持有，淘汰它会造成同一会话两把锁）。
                if len(self._session_locks) >= AI_MAX_SESSION_LOCKS:
                    for stale_id, stale in list(self._session_locks.items()):
                        if stale is entry or stale.refs > 0:
                            continue
                        del self._session_locks[stale_id]
                        if len(self._session_locks) < AI_MAX_SESSION_LOCKS:
                            break
                entry = _SessionLockEntry()
                self._session_locks[session_id] = entry
            entry.refs += 1
            return entry

    def chat_stream(self, request: AiChatRequest) -> Iterator[dict[str, Any]]:
        config = self._config_store.load()
        if not config.is_configured():
            raise AiNotConfigured("AI 服务尚未配置，请先在设置中填写 base_url、API Key 和模型名。")
        # 会话级互斥：上一轮 worker 仍在运行（例如用户点了“停止”但模型流尚未
        # 结束）时，新一轮先等待——两个 worker 并发写同一会话会互相覆盖并产生
        # 悬空 tool_calls，那是“中断后失忆”的根源之一。
        session_lock: _SessionLockEntry | None = None
        worker_started = False
        lock_held = False
        lock_released = False

        def drop_session_ref() -> None:
            """只归还引用计数，不动锁。

            ``acquire`` 失败（超时）的路径上本线程**从未**持有锁，此时释放锁
            会解开别人的锁（``threading.Lock`` 不做持有者校验），造成互斥失效。
            """
            if session_lock is None:
                return
            with self._session_locks_guard:
                session_lock.refs = max(0, session_lock.refs - 1)

        def release_session_lock() -> None:
            """释放会话锁并归还引用；幂等，可安全地在多条路径上兜底调用。"""
            nonlocal lock_released
            if session_lock is None or lock_released:
                return
            lock_released = True
            try:
                if lock_held:
                    session_lock.release()
            finally:
                drop_session_ref()

        candidate_id = sanitize_session_id(request.session_id) if request.session_id else None
        if candidate_id:
            session_lock = self._session_lock(candidate_id)
            if not session_lock.acquire(timeout=AI_SESSION_LOCK_TIMEOUT_SECONDS):
                release_session_lock()
                raise AiSessionBusy("上一轮回答仍在生成中，请稍候再发送新消息。")
            lock_held = True
        try:
            session = (
                self._sessions.get(request.session_id) if request.session_id else None
            ) or self._sessions.create(title=request.message[:20])
            session_id = str(session.get("session_id"))
            if session_lock is None:
                session_lock = self._session_lock(session_id)
                session_lock.acquire()
                lock_held = True
            yield {"type": "session", "session_id": session_id, "title": session.get("title")}

            system_prompt = f"{build_system_prompt(self._knowledge.is_ready(), config.research_style)}\n\n{today_context()}"
            profile = self._memory.profile_context()
            if profile:
                system_prompt += f"\n\n## 用户画像（长期记忆，越用越准）\n{profile}"
            facts = self._memory.facts_context()
            if facts:
                system_prompt += f"\n\n## 已知用户事实（长期记忆）\n{facts}"

            events: queue.Queue[dict[str, Any] | None] = queue.Queue()
            error_holder: list[dict[str, Any]] = []

            def on_event(event: dict[str, Any]) -> None:
                events.put(event)

            def worker() -> None:
                try:
                    artifacts = self._agent.run(
                        session=session,
                        user_message=request.message,
                        system_prompt=system_prompt,
                        max_steps=config.max_steps,
                        context=request.context.model_dump() if request.context else None,
                        on_event=on_event,
                    )
                    events.put(
                        {
                            "type": "result",
                            "session_id": session_id,
                            "display": session.get("display", []),
                            "strategy": artifacts.get("strategy"),
                            "chart": artifacts.get("chart"),
                            "updated_at": datetime.now(UTC).isoformat(),
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - converted to a stable error event
                    error_holder.append({"type": "error", "code": ai_error_code(exc), "message": str(exc)})
                finally:
                    # 哨兵必须在 finally 里：save/其他异常不能挂死消费端线程
                    try:
                        self._sessions.save(session)
                    finally:
                        events.put(None)
                        # 会话锁由 worker 释放：客户端断开（停止按钮）后 worker 仍在跑，
                        # 提前释放会让下一个请求与它并发写同一会话。
                        release_session_lock()
                if error_holder:
                    return
                # 长期记忆提取在哨兵之后的独立 daemon 线程，绝不阻塞事件流
                threading.Thread(
                    target=self._remember_from, args=(session,), name="ai-memory-extract", daemon=True
                ).start()

            thread = threading.Thread(target=worker, name="ai-agent-run", daemon=True)
            try:
                thread.start()
                worker_started = True
            except Exception:
                # worker 没起来，锁没人替我们释放（release_session_lock 幂等，
                # 下方 finally 再调一次也不会双重 release）
                release_session_lock()
                raise
            while True:
                try:
                    event = events.get(timeout=AI_STREAM_HEARTBEAT_SECONDS)
                except queue.Empty:
                    # 一次工具调用（尤其全市场多年的回测）可以几分钟不产生任何事件，
                    # 而前端按"多久没收到字节"判定空闲超时：静默会被误杀成"回答中断"，
                    # 之后 worker 仍在跑并持着会话锁，用户下一次发送要白等 90 秒。
                    yield {"type": "heartbeat", "session_id": session_id}
                    continue
                if event is None:
                    break
                yield event
            if error_holder:
                yield error_holder[0]
        finally:
            # 只有 worker 从未启动时才在这里释放锁；worker 已启动的路径由
            # worker 自己在 finally 里释放（客户端断开后它仍在运行）。
            if not worker_started:
                release_session_lock()

    def _remember_from(self, session: dict[str, Any]) -> None:
        """Best-effort long-term memory consolidation after a completed turn.

        One LLM call plans add/update/delete operations against the existing
        store, so memories consolidate (mem0-style) instead of piling up.
        """
        try:
            if not self._config_store.load().is_configured():
                return
            turns = session.get("display", [])[-4:]
            dialogue = "\n".join(f"{turn.get('role')}: {str(turn.get('content'))[:400]}" for turn in turns)
            ops = plan_memory_ops(self._model, dialogue, self._memory.load())
            if ops:
                applied = self._memory.apply_ops(ops)
                if applied:
                    try:
                        self._log("info", f"AI 长期记忆已更新：{applied} 条操作")
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001 - memory must never break a chat turn
            pass

    def delete_session(self, session_id: str) -> bool:
        """Delete a stored session.

        Refuses while that session still has a turn generating: the worker saves
        the session in its ``finally`` block, which would resurrect the file
        right after the delete and leave a "deleted" transcript on screen.
        """
        safe = sanitize_session_id(session_id)
        if safe is None:
            return False
        with self._session_locks_guard:
            entry = self._session_locks.get(safe)
            busy = entry is not None and entry.lock.locked()
        if busy:
            raise AiSessionBusy("该会话仍在生成回答，请先停止后再删除。")
        return self._sessions.delete(safe)

    def list_sessions(self) -> list[dict[str, Any]]:
        return self._sessions.list_sessions()

    def session_view(self, session_id: str) -> dict[str, Any]:
        """Read one stored session back for the UI.

        Only display turns cross the wire: protocol messages, the pending
        archive and the rolling summary stay server-side, so a restored
        transcript cannot be edited into model instructions.
        """
        session = self._sessions.get(session_id) if session_id else None
        if session is None:
            raise AiSessionNotFound("会话不存在或已被删除，将从新会话开始。")
        return {
            "session_id": str(session.get("session_id")),
            "title": str(session.get("title") or "新会话"),
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
            "display": session.get("display") or [],
        }

    # --------------------------------------------------------------- reports
    def list_reports(self) -> dict[str, Any]:
        items = self._report_store.list()
        return {
            "items": [
                {"name": item.name, "size": item.size, "created_at": item.created_at}
                for item in items[:60]
            ]
        }

    def read_report(self, name: str) -> dict[str, Any]:
        content = self._report_store.read(name)
        if content is None:
            raise AiError(f"报告不存在或文件名不合法：{name}")
        return {"name": name, "content": content}

    def overfit_check(self, payload: dict[str, Any]) -> dict[str, Any]:
        metrics = payload.get("metrics")
        if not isinstance(metrics, dict):
            raise ValueError("缺少回测指标 metrics。")
        combos = payload.get("combos") if isinstance(payload.get("combos"), list) else None
        return assess_overfit(metrics, combos=combos)

    # ---------------------------------------------------------------- events
    def events_stream(self) -> Iterator[dict[str, Any]]:
        stream = self._broker.subscribe()
        try:
            while True:
                try:
                    event = stream.get(timeout=HEARTBEAT_INTERVAL_SECONDS)
                    yield event
                except queue.Empty:
                    yield {"type": "heartbeat", "timestamp": datetime.now(UTC).isoformat()}
        finally:
            self._broker.unsubscribe(stream)
