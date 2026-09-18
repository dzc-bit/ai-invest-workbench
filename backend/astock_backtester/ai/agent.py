"""Agent loop: plan -> tool -> observe -> ... -> answer, streamed as events.

The loop is a plain state machine over the OpenAI tool-call protocol and only
depends on the :class:`~astock_backtester.ai.llm_client.ChatModel` protocol,
the :class:`ToolRegistry` and the context helpers — which makes it fully unit
testable with a scripted fake model and zero network.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, NamedTuple

from astock_backtester.ai.context import (
    ContextBudget,
    ToolResult,
    ToolResultStore,
    compact_context_payload,
    wrap_untrusted,
)
from astock_backtester.ai.llm_client import ChatModel
from astock_backtester.ai.prompts import build_compaction_messages, build_final_answer_messages
from astock_backtester.ai.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

AgentEvent = dict[str, Any]
EventHandler = Callable[[AgentEvent], None]

# 短期窗口按“条数”与“字符数”双阈值控制：只有两者都未超限时才不压缩，
# 避免每轮工具对话（assistant+tool 消息膨胀很快）都在新问题开始时触发压缩。
SHORT_TERM_WINDOW = 24
SHORT_TERM_MAX_CHARS = 36_000
# 归档攒批：攒够足够多的待压缩内容才调用一次纪要模型，避免频繁压缩。
CONSOLIDATE_MIN_ENTRIES = 12
CONSOLIDATE_MIN_CHARS = 6_000
# 归档条目上限：压缩持续失败（模型不可用等）时 pending_archive 只增不减，
# 这是长久的内存/落盘膨胀风险，按条数兜底裁剪。
ARCHIVE_MAX_ENTRIES = 400

# 摘要里含爬取正文的工具：digest 进入上下文前必须套不可信分隔符（AGENTS.md §15-8）
UNTRUSTED_DIGEST_TOOLS = frozenset(
    {"market_news", "market_briefing", "stock_research_reports", "dragon_tiger_board", "limit_up_pool"}
)

# 必须独占执行、不与其他工具并发的两个工具：update_stock_data 是唯一写路径（并发写
# 同一数据仓会撞 parquet 分区），run_strategy_backtest 把整表读进 pandas（并发等于
# 双份内存 + 磁盘 IO 抢占）。其余都是只读查询，可以并发。
SERIAL_TOOLS = frozenset({"update_stock_data", "run_strategy_backtest"})
MAX_PARALLEL_TOOLS = 4


class _ToolCall(NamedTuple):
    """一个已解析的 tool_call；``arguments`` 保留原文，registry 要靠它回传参数提示。"""

    call_id: str
    name: str
    arguments: str
    args: dict[str, Any]

    @staticmethod
    def of(call: dict[str, Any]) -> _ToolCall:
        function = call.get("function", {})
        arguments = str(function.get("arguments", ""))
        try:
            parsed = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            parsed = {}
        return _ToolCall(
            call_id=str(call.get("id", "")),
            name=str(function.get("name", "")),
            arguments=arguments,
            args=parsed if isinstance(parsed, dict) else {},
        )


class AgentRunner:
    def __init__(self, model: ChatModel, registry: ToolRegistry, result_store: ToolResultStore, budget: ContextBudget) -> None:
        self._model = model
        self._registry = registry
        self._result_store = result_store
        self._budget = budget

    # ------------------------------------------------------------------ run
    def run(
        self,
        *,
        session: dict[str, Any],
        user_message: str,
        system_prompt: str,
        max_steps: int,
        context: dict[str, Any] | None = None,
        on_event: EventHandler,
    ) -> dict[str, Any]:
        """Run one user turn to completion; returns UI artifacts (e.g. a
        runnable strategy JSON produced by a successful backtest tool call)."""
        # 实例属性会把并发运行的两个会话的工件串在一起（A 拿到 B 的回测策略），
        # 所以 artifacts 只活在单次 run 的作用域里。
        artifacts: dict[str, Any] = {}
        now = datetime.now(UTC).isoformat()
        # 上一次运行可能被中断（客户端断开/进程退出/模型异常），先修复悬空的
        # tool_calls——否则下次请求会被上游 API 以协议错误拒绝，表现为“失忆”。
        self._repair_interrupted_turn(session)
        content = user_message
        if context and context.get("kind") not in (None, "none"):
            content = f"{user_message}\n\n{compact_context_payload(str(context.get('kind')), context.get('payload') or {}, self._budget)}"
        session["messages"].append({"role": "user", "content": content})
        session["display"].append({"role": "user", "content": user_message, "ts": now})
        session["updated_at"] = now

        # Layered memory: hard short-term window; overflow is archived and then
        # consolidated into the rolling summary before the first model call.
        self._archive_overflow(session, on_event)
        self._consolidate_archive(session, on_event)
        schemas = self._registry.openai_schemas()

        for step in range(1, max_steps + 1):
            on_event({"type": "phase", "phase": f"思考中（第 {step}/{max_steps} 步）"})
            messages = self._build_request_messages(session, system_prompt)
            content_parts: list[str] = []
            for event in self._model.chat(messages, tools=schemas):
                if event[0] == "text":
                    content_parts.append(event[1])
                    on_event({"type": "token", "text": event[1]})
                    continue
                turn = event[1]
            tool_calls = turn.get("tool_calls")
            assistant_message: dict[str, Any] = {"role": "assistant", "content": turn.get("content")}
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            session["messages"].append(assistant_message)

            if not tool_calls:
                answer = turn.get("content") or ""
                if turn.get("truncated"):
                    # 只加在展示层：协议消息保留模型原文，不把 UI 提示回灌进后续上下文。
                    answer = f"{answer}\n\n（本回答因输出长度上限被截断，可回复“继续”或在设置里调大 max_tokens。）"
                session["display"].append(
                    {"role": "assistant", "content": answer, "tool_steps": [], "ts": datetime.now(UTC).isoformat()}
                )
                session["updated_at"] = datetime.now(UTC).isoformat()
                return artifacts

            steps = self._execute_tool_calls(session, tool_calls, on_event, artifacts)
            session["display"].append(
                {
                    "role": "assistant",
                    "content": "".join(content_parts),
                    "tool_steps": steps,
                    "ts": datetime.now(UTC).isoformat(),
                }
            )

        # 步数耗尽时绝不“空手中断”：强制做一次不带工具的收尾回答，
        # 把已收集的工具结果整理成结论交给用户。
        if self._forced_final_answer(session, system_prompt, on_event):
            return artifacts
        note = "（已达到单次问题的工具调用上限，且收尾回答生成失败；请拆小问题后重试。）"
        session["messages"].append({"role": "assistant", "content": note})
        session["display"].append({"role": "assistant", "content": note, "tool_steps": [], "ts": datetime.now(UTC).isoformat()})
        on_event({"type": "phase", "phase": "已达工具调用上限"})
        return artifacts

    # --------------------------------------------------------------- tools
    def _execute_tool_calls(
        self,
        session: dict[str, Any],
        tool_calls: list[dict[str, Any]],
        on_event: EventHandler,
        artifacts: dict[str, Any],
    ) -> list[dict[str, Any]]:
        plans = [_ToolCall.of(call) for call in tool_calls]
        for plan in plans:
            on_event({"type": "tool_call", "id": plan.call_id, "name": plan.name, "args": plan.args})
        executions = self._invoke(plans)

        steps: list[dict[str, Any]] = []
        for plan, execution in zip(plans, executions, strict=True):
            self._result_store.put(
                ToolResult(
                    call_id=plan.call_id,
                    name=plan.name,
                    arguments=plan.args,
                    payload=execution.payload,
                    summary=execution.summary,
                )
            )
            if plan.name == "run_strategy_backtest" and execution.ok:
                if isinstance(execution.payload.get("strategy"), dict):
                    artifacts["strategy"] = execution.payload["strategy"]
                curve = execution.payload.get("equity_curve_downsampled")
                if curve:
                    artifacts["chart"] = {
                        "type": "equity_curve",
                        "title": "回测权益曲线",
                        "points": curve,
                    }
            on_event(
                {
                    "type": "tool_result",
                    "id": plan.call_id,
                    "name": plan.name,
                    "ok": execution.ok,
                    "summary": execution.summary,
                    "duration_ms": execution.duration_ms,
                    "diagnostics": execution.diagnostics,
                }
            )
            steps.append(
                {
                    "id": plan.call_id,
                    "name": plan.name,
                    "ok": execution.ok,
                    "summary": execution.summary,
                    "duration_ms": execution.duration_ms,
                    "code": execution.code,
                }
            )
            session_tool_content = execution.summary
            if execution.code:
                # 失败类别跟着摘要进协议消息与会话文件：中断恢复和回放后要能区分
                # "改参数重试"/"换工具"/"先补数据"，而不是只留一句人读文案。
                session_tool_content = f"[code={execution.code}] {session_tool_content}"
            if execution.diagnostics:
                session_tool_content += "\n诊断: " + "；".join(execution.diagnostics[:3])
            payload = execution.payload if isinstance(execution.payload, dict) else {}
            more_rows = payload.get("more_rows")
            if more_rows:
                session_tool_content += (
                    f"\n（另有 {more_rows} 行未展开，可调用 "
                    f'read_tool_result(call_id="{plan.call_id}", offset={payload.get("shown_rows", 0)}) 续读）'
                )
            if plan.name in UNTRUSTED_DIGEST_TOOLS:
                session_tool_content = wrap_untrusted(session_tool_content)
            session_tool_content = self._budget.digest(session_tool_content, execution.digest_chars)
            session_tool = {"role": "tool", "tool_call_id": plan.call_id, "content": session_tool_content}
            self._session_messages_target(session).append(session_tool)
        return steps

    def _invoke(self, plans: list[_ToolCall]) -> list[Any]:
        """Execute one batch of tool calls, returning results in request order.

        Read-only batches fan out on a bounded pool; a batch containing a
        write or a full-frame backtest stays serial, and ordering is what keeps
        the assistant(tool_calls)->tool pairing ``_repair_interrupted_turn``
        relies on intact.
        """
        if len(plans) > 1 and not any(plan.name in SERIAL_TOOLS for plan in plans):
            with ThreadPoolExecutor(
                max_workers=min(MAX_PARALLEL_TOOLS, len(plans)), thread_name_prefix="ai-tool"
            ) as pool:
                return list(pool.map(lambda plan: self._registry.execute(plan.name, plan.arguments), plans))
        return [self._registry.execute(plan.name, plan.arguments) for plan in plans]

    # ----------------------------------------------------------- messages
    def _build_request_messages(self, session: dict[str, Any], system_prompt: str) -> list[dict[str, Any]]:
        system = system_prompt
        rolling = str(session.get("rolling_summary") or "")
        if rolling:
            system += f"\n\n## 会话纪要（更早的对话已压缩）\n{rolling}"
        return [{"role": "system", "content": system}, *session["messages"]]

    def _repair_interrupted_turn(self, session: dict[str, Any]) -> None:
        """Heal a session interrupted mid-tool-call.

        If the previous run died between an assistant ``tool_calls`` message and
        its tool results, the OpenAI-protocol history is invalid and every
        following request in this session would be rejected upstream — the user
        experiences this as the session "losing its memory".  Insert synthetic
        tool results for unanswered call ids so the next turn can proceed with
        the full history intact.
        """
        messages = session.get("messages") or []
        repaired: list[dict[str, Any]] = []
        index = 0
        changed = False
        while index < len(messages):
            message = messages[index]
            repaired.append(message)
            index += 1
            calls = message.get("tool_calls") or []
            if not calls:
                continue
            # 吃掉紧随其后的既有 tool 结果。
            while index < len(messages) and messages[index].get("role") == "tool":
                repaired.append(messages[index])
                index += 1
            answered = {
                str(item.get("tool_call_id")) for item in repaired if item.get("role") == "tool"
            }
            for call in calls:
                call_id = str(call.get("id", ""))
                if call_id in answered:
                    continue
                name = str(call.get("function", {}).get("name", ""))
                repaired.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": (
                            f"[code=interrupted] （上一次运行在调用 {name or '工具'} 时被中断，没有返回结果；"
                            "如需该数据请重新调用。）"
                        ),
                    }
                )
                changed = True
        if changed:
            session["messages"] = repaired

    def _forced_final_answer(self, session: dict[str, Any], system_prompt: str, on_event: EventHandler) -> bool:
        """One no-tools closing call after the step budget is exhausted."""
        on_event({"type": "phase", "phase": "工具步数已达上限，正在整理已有结果作答"})
        messages = self._build_request_messages(session, system_prompt)
        try:
            streamed: list[str] = []
            final_content = ""
            truncated = False
            for event in self._model.chat(build_final_answer_messages(messages), tools=None):
                if event[0] == "text":
                    # 只用于给前端实时出字：三协议的 final.content 就是这些分片的拼接，
                    # 两处都收会让落盘的收尾回答整段翻倍。
                    streamed.append(event[1])
                    on_event({"type": "token", "text": event[1]})
                elif event[0] == "final":
                    payload = event[1] or {}
                    final_content = str(payload.get("content") or "")
                    truncated = bool(payload.get("truncated"))
            answer = (final_content or "".join(streamed)).strip()
        except Exception:  # noqa: BLE001 - 收尾失败时回退到提示文案
            return False
        if not answer:
            return False
        session["messages"].append({"role": "assistant", "content": answer})
        session["display"].append(
            {
                "role": "assistant",
                "content": (
                    f"{answer}\n\n（本回答因输出长度上限被截断，可回复“继续”或在设置里调大 max_tokens。）"
                    if truncated
                    else answer
                ),
                "tool_steps": [],
                "ts": datetime.now(UTC).isoformat(),
            }
        )
        session["updated_at"] = datetime.now(UTC).isoformat()
        return True

    def _archive_overflow(self, session: dict[str, Any], on_event: EventHandler) -> None:
        """Keep the live window within both the message-count and char budget.

        The cut point is extended to the next user-message boundary so an
        assistant(tool_calls)/tool pair is never split across the window edge.
        """
        messages = session["messages"]
        overflow = max(len(messages) - SHORT_TERM_WINDOW, 0)
        total_chars = sum(len(str(message.get("content") or "")) for message in messages)
        if total_chars > SHORT_TERM_MAX_CHARS:
            # 字符超限：从最旧处开始归档，直到剩余字符回到阈值内。
            dropped_chars = 0
            for index in range(len(messages)):
                if total_chars - dropped_chars <= SHORT_TERM_MAX_CHARS:
                    break
                dropped_chars += len(str(messages[index].get("content") or ""))
                overflow = max(overflow, index + 1)
        if overflow <= 0:
            return
        # 把裁剪点推到下一个 user 边界，避免 assistant(tool_calls)/tool 配对被切断。
        # 但若后面**没有** user 消息（例如窗口里全是 assistant/tool），推到末尾会
        # 让整段字符预算静默失效（窗口仍严重超预算却不归档）。此时退回字符裁剪点。
        boundary = overflow
        while boundary < len(messages) and messages[boundary].get("role") != "user":
            boundary += 1
        overflow = boundary if boundary < len(messages) else overflow
        if overflow >= len(messages):
            return
        dropped = messages[:overflow]
        session["messages"] = messages[overflow:]
        archive = session.setdefault("pending_archive", [])
        for message in dropped:
            text = str(message.get("content") or "")
            calls = message.get("tool_calls") or []
            if calls:
                text += " " + ", ".join(call.get("function", {}).get("name", "") for call in calls)
            archive.append(f"{message.get('role')}: {text}")
        # 压缩持续失败时归档只增不减，必须按条数兜底裁剪，否则会话文件会无限膨胀。
        if len(archive) > ARCHIVE_MAX_ENTRIES:
            # 丢最旧的条目，但保留一条“已丢弃 N 条”的占位说明，避免静默失忆。
            # 说明本身占 1 条，所以保留 ARCHIVE_MAX_ENTRIES - 1 条正文。
            excess = len(archive) - (ARCHIVE_MAX_ENTRIES - 1)
            archive[:] = [f"（更早的 {excess} 条对话因压缩失败已丢弃）", *archive[excess:]]
        on_event({"type": "phase", "phase": "归档短期窗口之外的对话"})

    def _consolidate_archive(self, session: dict[str, Any], on_event: EventHandler) -> None:
        """Fold archived dialogue into the rolling summary (one model call).

        Compression is deliberately batched: small archives stay in
        ``pending_archive`` (persisted with the session, so nothing is lost)
        until they are large enough to be worth a summarization call.
        """
        archive = session.get("pending_archive") or []
        if not archive:
            return
        archive_chars = sum(len(item) for item in archive)
        if len(archive) < CONSOLIDATE_MIN_ENTRIES and archive_chars < CONSOLIDATE_MIN_CHARS:
            return
        on_event({"type": "phase", "phase": "压缩进长期会话纪要"})
        existing = str(session.get("rolling_summary") or "")
        lines = [f"（既有纪要）{existing}"] if existing else []
        lines.extend(archive)
        try:
            summary = self._summarize_history_text("\n".join(lines))
        except Exception:  # noqa: BLE001 - 压缩失败不能丢归档，也不能中断本轮
            logger.warning("会话归档压缩失败；保留 pending_archive 待下次重试", exc_info=True)
            return
        if not summary:
            # 模型返回空：保留归档内容，下次达到阈值再试。
            return
        # 只有压缩成功才清空，避免上游异常时归档内容被永久丢弃。
        session["pending_archive"] = []
        session["rolling_summary"] = summary[:4000]

    def _summarize_history_text(self, history_text: str) -> str:
        final_content = ""
        for event in self._model.chat(build_compaction_messages(history_text), tools=None):
            if event[0] == "final":
                final_content = str(event[1].get("content") or "")
        return final_content[:4000]

    def _session_messages_target(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        return session["messages"]
