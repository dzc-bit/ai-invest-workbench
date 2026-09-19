"""Agent tool registry: JSON schema + executor + summarizer per tool.

Every tool is read-only by construction: executors may only call public
provider/warehouse read APIs.  ``summarizer`` produces the compact digest that
enters the model context; the full payload stays in :class:`ToolResultStore`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from astock_backtester.ai.context import retain_rows

Executor = Callable[[dict[str, Any]], dict[str, Any]]
Summarizer = Callable[[dict[str, Any]], str]


@dataclass
class AiTool:
    name: str
    description: str
    parameters: dict[str, Any]
    executor: Executor
    summarizer: Summarizer
    read_only: bool = True
    # 表格/榜单类工具的摘要预算（0 = 用 ContextBudget 默认）：默认 1200 字会把
    # 20 行摘要切成 8 行，模型于是"看不见"它刚查到的数据。
    digest_chars: int = 0

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# 小而稳定的失败分类。存在理由是让失败类别能进协议 tool 消息与会话文件：
# 中断恢复/回放后要能区分"改参数重试"、"换工具"和"先补数据"，而不是只给人看文案。
CODE_UNKNOWN_TOOL = "unknown_tool"
CODE_BAD_ARGUMENTS = "bad_arguments"
CODE_TOOL_ERROR = "tool_error"
CODE_NO_DATA = "no_data"


@dataclass
class ToolExecution:
    ok: bool
    payload: dict[str, Any]
    summary: str
    duration_ms: int
    diagnostics: list[str] = field(default_factory=list)
    code: str = ""
    digest_chars: int = 0


READ_PAGE_ROWS = 40
MAX_PAGE_ROWS = 80


def build_read_result_tool(store: Any) -> AiTool:
    """让模型把"另有 M 行未展开"变成可执行的下一步，而不是凭半截数据下结论。"""

    def read_tool_result(args: dict[str, Any]) -> dict[str, Any]:
        call_id = str(args.get("call_id", "")).strip()
        if not call_id:
            return {"ok": False, "error_code": CODE_BAD_ARGUMENTS, "error": "call_id 不能为空"}
        stored = store.get(call_id)
        if stored is None:
            return {
                "ok": False,
                "error_code": "result_evicted",
                "error": "该次结果已不在内存（超出保留上限或服务已重启），不是数据不存在；请重新调用原工具。",
            }
        rows = stored.payload.get("rows") if isinstance(stored.payload, dict) else None
        if not isinstance(rows, list) or not rows:
            return {
                "ok": False,
                "error_code": "not_rowset",
                "error": f"{stored.name} 的结果不是行集，之前的摘要已包含全部要点；确实缺数据请换工具或放宽查询条件。",
            }
        try:
            offset = max(0, int(args.get("offset", 0)))
            limit = max(1, min(int(args.get("rows", READ_PAGE_ROWS)), MAX_PAGE_ROWS))
        except (TypeError, ValueError):
            return {"ok": False, "error_code": CODE_BAD_ARGUMENTS, "error": "offset 与 rows 必须是整数"}
        page = [row for row in rows[offset : offset + limit] if isinstance(row, dict)]
        if not page:
            return {
                "ok": True,
                "name": stored.name,
                "offset": offset,
                "total": len(rows),
                "shown_rows": 0,
                "table": "（该偏移已越过结果末尾）",
            }
        retained = retain_rows(page, max_rows=limit)
        return {
            "ok": True,
            "name": stored.name,
            "offset": offset,
            "total": len(rows),
            "shown_rows": retained.kept,
            "more_rows": max(len(rows) - (offset + retained.kept), 0),
            "table": retained.text,
        }

    def summarize_read(payload: dict[str, Any]) -> str:
        if not payload.get("ok"):
            return f"回读失败：{payload.get('error')}"
        shown = int(payload.get("shown_rows") or 0)
        offset = int(payload.get("offset") or 0)
        head = f"{payload.get('name')} 结果第 {offset + 1}~{offset + shown} 行（共 {payload.get('total')} 行）："
        more = int(payload.get("more_rows") or 0)
        tail = (
            f"\n（还剩 {more} 行未读，可继续 offset={offset + shown}）"
            if more
            else "\n（该结果已全部读完）"
        )
        return f"{head}\n{payload.get('table')}{tail}"

    return AiTool(
        name="read_tool_result",
        description=(
            "按行续读上一次工具结果的完整行集。当某个工具返回的摘要末尾写着"
            "\u201c另有 M 行未展开\u201d时用它取回后续行，不要凭半截数据下结论，也不要重复调用原工具。"
            "参数里的 call_id 就是那次工具调用的 id。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "call_id": {"type": "string", "description": "要续读的那次工具调用 id"},
                "offset": {"type": "integer", "description": "从第几行开始（0 起算），默认 0"},
                "rows": {"type": "integer", "description": "本次读取行数，默认 40，最大 80"},
            },
            "required": ["call_id"],
        },
        executor=read_tool_result,
        summarizer=summarize_read,
        digest_chars=3_600,
    )


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, AiTool] = {}

    def register(self, tool: AiTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def register_all(self, tools: list[AiTool]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> AiTool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def openai_schemas(self) -> list[dict[str, Any]]:
        return [tool.openai_schema() for tool in self._tools.values()]

    def _param_hint(self, name: str) -> str:
        """Human-readable parameter reminder used in failure feedback so the
        model can self-correct on its next attempt instead of failing again."""
        tool = self._tools.get(name)
        if tool is None:
            return ""
        properties = (tool.parameters or {}).get("properties") or {}
        required = set((tool.parameters or {}).get("required") or [])
        if not properties:
            return "本工具不需要参数"
        parts = []
        for key, schema in list(properties.items())[:6]:
            mark = "*" if key in required else ""
            description = str(schema.get("description", ""))[:40] if isinstance(schema, dict) else ""
            parts.append(f'"{key}"{mark}: {description}')
        suffix = "（* 为必填）" if required else ""
        return f"参数格式：{{{'; '.join(parts)}}}{suffix}"

    def execute(self, name: str, arguments: str) -> ToolExecution:
        started = time.monotonic()
        tool = self._tools.get(name)
        if tool is None:
            available = ", ".join(list(self._tools)[:6])
            total = len(self._tools)
            return ToolExecution(
                False,
                {"ok": False, "error": f"未知工具：{name}"},
                f"未知工具 {name}（可用工具共 {total} 个，例如：{available}）。请从工具列表中选择正确名称重试。",
                0,
                code=CODE_UNKNOWN_TOOL,
            )
        try:
            args = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return ToolExecution(
                False,
                {"ok": False, "error": f"参数不是合法 JSON：{exc}"},
                f"参数解析失败：{exc}。{self._param_hint(name)}",
                0,
                code=CODE_BAD_ARGUMENTS,
            )
        if not isinstance(args, dict):
            return ToolExecution(
                False,
                {"ok": False, "error": "工具参数必须是 JSON 对象"},
                f"参数格式错误：工具参数必须是 JSON 对象。{self._param_hint(name)}",
                0,
                code=CODE_BAD_ARGUMENTS,
            )
        try:
            payload = tool.executor(args)
        except Exception as exc:  # noqa: BLE001 - tool failures must not kill the agent loop
            payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        duration_ms = int((time.monotonic() - started) * 1000)
        ok = bool(payload.get("ok", False))
        try:
            summary = tool.summarizer(payload) if ok else f"调用失败：{payload.get('error', '未知错误')}"
            if not ok:
                summary = f"{summary}（{self._param_hint(name)}）" if self._param_hint(name) else summary
        except Exception:  # noqa: BLE001 - summarizer bugs must not kill the loop either
            summary = "工具结果摘要生成失败"
        diagnostics = [str(item) for item in payload.get("diagnostics", []) if item]
        return ToolExecution(
            ok, payload, summary, duration_ms, diagnostics, code=_failure_code(payload, ok), digest_chars=tool.digest_chars
        )


def _failure_code(payload: dict[str, Any], ok: bool) -> str:
    """失败类别：优先采用工具自报的 error_code，否则按错误文案粗分。"""
    if ok:
        return ""
    declared = str(payload.get("error_code") or "").strip()
    if declared:
        return declared
    error = str(payload.get("error") or "")
    if any(marker in error for marker in ("没有", "无数据", "还没有", "请先在数据中心", "无该标的")):
        return CODE_NO_DATA
    return CODE_TOOL_ERROR
