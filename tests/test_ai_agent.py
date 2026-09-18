from __future__ import annotations

import threading
from typing import Any

from astock_backtester.ai.agent import SHORT_TERM_MAX_CHARS, AgentRunner
from astock_backtester.ai.context import ContextBudget, ToolResultStore
from astock_backtester.ai.tools.registry import AiTool, ToolRegistry


class FakeModel:
    """Scripted ChatModel: pops scripted turn sequences, repeats the last one."""

    def __init__(self, script: list[list[tuple[str, Any]]]) -> None:
        self.script = script
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None):
        self.calls.append({"messages": messages, "tools": tools})
        if not self.script:
            item = self._last
        else:
            item = self.script.pop(0)
            self._last = item
        yield from item

    _last: list[tuple[str, Any]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text))] for text in texts]


def _final(content: str | None = None, tool_calls: list[dict[str, Any]] | None = None):
    return ("final", {"content": content, "tool_calls": tool_calls})


def _tool_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        AiTool(
            name="echo_tool",
            description="echo",
            parameters={"type": "object", "properties": {}},
            executor=lambda args: {"ok": True, "value": args.get("x"), "diagnostics": ["d1"]},
            summarizer=lambda payload: f"value={payload.get('value')}",
        )
    )
    return registry


def _session() -> dict[str, Any]:
    return {
        "session_id": "s1",
        "title": "新会话",
        "created_at": "now",
        "updated_at": "now",
        "rolling_summary": "",
        "messages": [],
        "display": [],
    }


def test_agent_runs_tool_then_answers():
    model = FakeModel(
        [
            [_final(tool_calls=[_tool_call("t1", "echo_tool", '{"x": 7}')])],
            [_final(content="结论 7")],
        ]
    )
    runner = AgentRunner(model, _registry(), ToolResultStore(), ContextBudget())
    events: list[dict[str, Any]] = []
    session = _session()
    runner.run(
        session=session,
        user_message="看看 7",
        system_prompt="SYS",
        max_steps=4,
        on_event=events.append,
    )
    types = [event["type"] for event in events]
    assert types[0] == "phase"
    assert "tool_call" in types and "tool_result" in types
    tool_result = next(event for event in events if event["type"] == "tool_result")
    assert tool_result["ok"] is True and tool_result["summary"] == "value=7"
    tool_call = next(event for event in events if event["type"] == "tool_call")
    assert tool_call["args"] == {"x": 7}

    tool_messages = [message for message in session["messages"] if message.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["content"].startswith("value=7")  # digest only, no full payload
    assert session["display"][-1]["content"] == "结论 7"
    assert session["display"][-2]["tool_steps"][0]["name"] == "echo_tool"

    # full payload kept in the store, referenced by call id
    second_model_call = model.calls[-1]
    tool_messages_in_request = [m for m in second_model_call["messages"] if m.get("role") == "tool"]
    assert tool_messages_in_request[0]["content"].startswith("value=7")


def test_agent_runs_read_only_tool_batch_concurrently_in_order():
    """只读工具批次并发执行，但协议消息仍按 tool_calls 原顺序落盘。

    Barrier 是并发的硬证明：两个执行器没有同时在跑就会 BrokenBarrierError，被
    registry 吞成 ok=False，下面的 value 断言随即变红——不需要脆弱的计时断言。
    顺序部分保护的是 _repair_interrupted_turn 依赖的 assistant(tool_calls)→tool 配对。
    """
    gate = threading.Barrier(2, timeout=5)

    def executor(args: dict[str, Any]) -> dict[str, Any]:
        gate.wait()
        return {"ok": True, "value": args.get("x"), "diagnostics": []}

    registry = ToolRegistry()
    registry.register(
        AiTool(
            name="barrier_tool",
            description="blocks until both calls run",
            parameters={"type": "object", "properties": {}},
            executor=executor,
            summarizer=lambda payload: f"value={payload.get('value')}",
        )
    )
    model = FakeModel(
        [
            [
                _final(
                    tool_calls=[
                        _tool_call("t1", "barrier_tool", '{"x": 1}'),
                        _tool_call("t2", "barrier_tool", '{"x": 2}'),
                    ]
                )
            ],
            [_final(content="并发结论")],
        ]
    )
    runner = AgentRunner(model, registry, ToolResultStore(), ContextBudget())
    session = _session()
    runner.run(session=session, user_message="并发看看", system_prompt="SYS", max_steps=2, on_event=lambda _: None)

    tool_messages = [message for message in session["messages"] if message.get("role") == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == ["t1", "t2"]
    assert [message["content"] for message in tool_messages] == ["value=1", "value=2"]


def test_agent_stops_at_max_steps():
    model = FakeModel([[_final(tool_calls=[_tool_call("t1", "echo_tool", '{"x": 1}')])] for _ in range(4)])
    runner = AgentRunner(model, _registry(), ToolResultStore(), ContextBudget())
    events: list[dict[str, Any]] = []
    session = _session()
    runner.run(session=session, user_message="q", system_prompt="SYS", max_steps=2, on_event=events.append)
    assert any("上限" in str(message.get("content")) for message in session["messages"])
    assert any(event["type"] == "phase" and "上限" in event.get("phase", "") for event in events)


def test_agent_max_steps_still_delivers_final_answer():
    """步数耗尽时不再空手中断：强制做一次无工具收尾回答。"""
    tool_only = [_final(tool_calls=[_tool_call("t1", "echo_tool", '{"x": 1}')])]
    # 真实客户端收尾时会先发 text 分片、再发 final，且 final.content 就是分片拼接；
    # 只发 final 的脚本会漏掉"分片+final 双写"这类膨胀 bug。
    closing = [("text", "基于已有"), ("text", "结果的最终结论"), _final(content="基于已有结果的最终结论")]
    model = FakeModel([tool_only, tool_only, closing])
    runner = AgentRunner(model, _registry(), ToolResultStore(), ContextBudget())
    events: list[dict[str, Any]] = []
    session = _session()
    runner.run(session=session, user_message="q", system_prompt="SYS", max_steps=2, on_event=events.append)
    # 前两步都是工具调用 → 收尾调用不带工具
    closing_call = model.calls[-1]
    assert closing_call["tools"] is None
    assert session["display"][-1]["content"] == "基于已有结果的最终结论"
    assert any("整理已有结果" in str(event.get("phase", "")) for event in events)
    # 收尾请求带“最终回答”指令
    system_texts = [str(message.get("content")) for message in closing_call["messages"] if message["role"] == "system"]
    assert any("工具调用步数已达到本次上限" in text for text in system_texts)


def test_agent_repairs_dangling_tool_calls_from_interrupted_run():
    """被中断的会话留下悬空 tool_calls 时，下一轮先合成占位工具结果，协议恢复有效。"""
    model = FakeModel([[_final(content="续上：结论")]])
    runner = AgentRunner(model, _registry(), ToolResultStore(), ContextBudget())
    session = _session()
    session["messages"].extend(
        [
            {"role": "user", "content": "上一轮问题"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _tool_call("t1", "echo_tool", '{"x": 1}'),
                    _tool_call("t2", "echo_tool", '{"x": 2}'),
                ],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "value=1"},
            # t2 的结果缺失（运行在中途被打断）
        ]
    )
    events: list[dict[str, Any]] = []
    runner.run(session=session, user_message="继续", system_prompt="SYS", max_steps=2, on_event=events.append)

    tool_messages = [message for message in session["messages"] if message.get("role") == "tool"]
    assert len(tool_messages) == 2
    assert tool_messages[0]["tool_call_id"] == "t1"
    assert tool_messages[1]["tool_call_id"] == "t2"
    assert "中断" in tool_messages[1]["content"]
    # 修复后的消息紧跟在 assistant(tool_calls) 之后，顺序有效
    request_messages = model.calls[0]["messages"]
    roles = [message["role"] for message in request_messages]
    assert roles.index("tool") > roles.index("assistant")


def test_agent_repair_keeps_intact_sessions_unchanged():
    model = FakeModel([[_final(content="答案")]])
    runner = AgentRunner(model, _registry(), ToolResultStore(), ContextBudget())
    session = _session()
    session["messages"].extend(
        [
            {"role": "user", "content": "问题"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("t1", "echo_tool", "{}")],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "value=None"},
        ]
    )
    before = [dict(message) for message in session["messages"]]
    runner.run(session=session, user_message="继续", system_prompt="SYS", max_steps=2, on_event=lambda event: None)
    assert session["messages"][:3] == before


def test_agent_archives_overflow_and_consolidates_summary():
    model = FakeModel([[_final(content="纪要内容")], [_final(content="最终答案")]])
    runner = AgentRunner(model, _registry(), ToolResultStore(), ContextBudget())
    session = _session()
    for index in range(40):
        session["messages"].append({"role": "user", "content": f"历史消息 {index} " + "x" * 500})
    events: list[dict[str, Any]] = []
    runner.run(session=session, user_message="继续", system_prompt="SYS", max_steps=3, on_event=events.append)

    assert session["rolling_summary"] == "纪要内容"
    assert session.get("pending_archive") == []
    # 短期窗口 24 条：40 条旧消息 + 新用户消息 → 归档 17 条（≥12 触发压缩），
    # 保留 24 条窗口，再加回答 1 条
    assert len(session["messages"]) == 25
    assert any(event["type"] == "phase" and "归档" in event.get("phase", "") for event in events)
    assert any(event["type"] == "phase" and "压缩" in event.get("phase", "") for event in events)
    request_messages = model.calls[-1]["messages"]
    assert any("会话纪要" in str(message.get("content")) for message in request_messages if message["role"] == "system")


def test_agent_defers_small_archives_to_batch_compression():
    model = FakeModel([[_final(content="最终答案")]])
    runner = AgentRunner(model, _registry(), ToolResultStore(), ContextBudget())
    session = _session()
    for index in range(27):
        session["messages"].append({"role": "user", "content": f"历史 {index}"})
    events: list[dict[str, Any]] = []
    runner.run(session=session, user_message="最新问题", system_prompt="SYS", max_steps=2, on_event=events.append)

    # 归档发生（27+1−24=4 条越过窗口），但 4 条 < 12 条攒批阈值 → 不调用纪要模型，
    # 归档暂存 pending_archive（随会话落盘，内容不丢），压缩延后到攒够再一次性做。
    assert session["rolling_summary"] == ""
    archived = session.get("pending_archive") or []
    assert len(archived) >= 1
    assert len(model.calls) == 1  # 只有回答调用，没有压缩调用
    assert any(event["type"] == "phase" and "归档" in event.get("phase", "") for event in events)
    assert not any(event["type"] == "phase" and "压缩" in event.get("phase", "") for event in events)


def test_agent_window_stays_intact_below_threshold_without_archived_turns():
    model = FakeModel([[_final(content="答案")]])
    runner = AgentRunner(model, _registry(), ToolResultStore(), ContextBudget())
    session = _session()
    for index in range(9):
        session["messages"].append({"role": "user", "content": f"历史 {index}"})
    runner.run(session=session, user_message="最新问题", system_prompt="SYS", max_steps=2, on_event=lambda event: None)
    # 未超窗口 → 不归档、不调用纪要模型；窗口内 10 条 + 回答 1 条
    assert session["rolling_summary"] == ""
    assert len(model.calls) == 1
    request_messages = model.calls[0]["messages"]
    assert len(request_messages) == 11  # 1 system + 10 窗口消息


def test_agent_keeps_pending_archive_when_compaction_model_fails():
    """压缩调用抛异常时，归档内容必须保留、本轮不得中断。

    曾经 `pending_archive` 在调用模型前就被清空，上游一旦抛错（网络/500/超时）
    归档内容永久丢失，用户表现为“AI 忘了之前聊的内容”。
    """

    class ExplodingModel(FakeModel):
        def chat(self, messages, *, tools=None):
            self.calls.append({"messages": messages, "tools": tools})
            raise RuntimeError("upstream exploded")
            yield  # pragma: no cover

    model = ExplodingModel([])
    runner = AgentRunner(model, _registry(), ToolResultStore(), ContextBudget())
    session = _session()
    for index in range(27):
        session["messages"].append({"role": "user", "content": f"历史 {index} x" * 20})
    # 直接把归档攒到超过阈值，确保会进入压缩分支
    session["pending_archive"] = ["归档条目 " + "y" * 200 for _ in range(20)]
    before = list(session["pending_archive"])

    events: list[dict[str, Any]] = []
    runner._consolidate_archive(session, events.append)

    # 压缩失败 → 不抛异常、归档原样保留、纪要不变
    assert session["pending_archive"] == before
    assert session["rolling_summary"] == ""


def test_agent_clears_pending_archive_only_after_successful_compaction():
    model = FakeModel([[_final(content="纪要内容")], [_final(content="答案")]])
    runner = AgentRunner(model, _registry(), ToolResultStore(), ContextBudget())
    session = _session()
    session["pending_archive"] = ["归档条目 " + "y" * 200 for _ in range(20)]

    runner._consolidate_archive(session, lambda event: None)

    assert session["rolling_summary"] == "纪要内容"
    assert session.get("pending_archive") == []


def test_agent_caps_pending_archive_when_compaction_keeps_failing():
    """压缩持续失败时归档必须有条数上限（否则会话文件无限膨胀）。

    修复“压缩失败丢归档”时保留了 pending_archive，但若上游长期不可用，归档
    只增不减。这里验证兜底裁剪生效，且裁剪会留下可见的“已丢弃 N 条”说明。
    """
    from astock_backtester.ai.agent import ARCHIVE_MAX_ENTRIES

    model = FakeModel([[_final(content="答案")]])
    runner = AgentRunner(model, _registry(), ToolResultStore(), ContextBudget())
    session = _session()
    session["messages"] = [{"role": "user", "content": f"第 {i} 轮 " + "z" * 900} for i in range(ARCHIVE_MAX_ENTRIES + 60)]

    runner._archive_overflow(session, lambda event: None)

    archived = session["pending_archive"]
    assert len(archived) <= ARCHIVE_MAX_ENTRIES
    # 首条是“已丢弃 N 条”的占位说明，不是静默截断。
    assert "已丢弃" in archived[0]


def test_agent_char_budget_archives_when_no_user_boundary_follows():
    """字符预算不得因为没有 user 边界就静默失效（S1 形态 A）。

    裁剪点会被推到下一个 user 边界，以免切断 assistant(tool_calls)/tool 配对。
    但窗口里全是 assistant/tool 消息（工具长跑很常见）时，推到末尾会让整个
    字符预算变成空操作 —— 25 条 x 5000 字 = 125k 字仍留在窗口里，远超 36k 预算。
    """
    model = FakeModel([[_final(content="答案")]])
    runner = AgentRunner(model, _registry(), ToolResultStore(), ContextBudget())
    session = _session()
    session["messages"] = [{"role": "assistant", "content": "a" * 5000} for _ in range(25)]

    runner._archive_overflow(session, lambda event: None)

    remaining = sum(len(str(m.get("content") or "")) for m in session["messages"])
    assert remaining <= SHORT_TERM_MAX_CHARS, f"字符预算被静默跳过：窗口仍剩 {remaining} 字"
    assert len(session.get("pending_archive") or []) > 0


def test_agent_avoids_splitting_tool_pair_at_window_edge():
    """对照守卫：有 user 边界时必须推到边界，不得把 tool 配对被切断。"""
    runner = AgentRunner(FakeModel([]), _registry(), ToolResultStore(), ContextBudget())
    session = _session()
    # 26 条 → 条数裁剪点 index=2；messages[2] 是 tool，边界应推到 index=3 的 user。
    session["messages"] = [
        {"role": "user", "content": "q0"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "x"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
        {"role": "user", "content": "q1"},
    ] + [{"role": "user", "content": f"fill {i}"} for i in range(22)]

    runner._archive_overflow(session, lambda event: None)

    archived = session.get("pending_archive") or []
    assert len(archived) == 3, archived
    # 前三条整组被归档：user + assistant(tool_calls) + tool，配对完整。
    assert archived[0].startswith("user:")
    assert archived[1].startswith("assistant:")
    assert archived[2].startswith("tool:")
    assert session["messages"][0]["content"] == "q1"


def test_agent_backtest_tool_yields_strategy_and_chart():
    registry = ToolRegistry()
    registry.register(
        AiTool(
            name="run_strategy_backtest",
            description="backtest",
            parameters={"type": "object", "properties": {}},
            executor=lambda args: {
                "ok": True,
                "strategy": {"name": "AI 生成策略"},
                "equity_curve_downsampled": [
                    {"trade_date": "2026-01-05", "equity": 1_000_000, "cash": 400_000, "market_value": 600_000, "drawdown_pct": -0.01}
                ],
            },
            summarizer=lambda payload: "回测完成",
        )
    )
    model = FakeModel(
        [
            [_final(tool_calls=[_tool_call("t1", "run_strategy_backtest", "{}")])],
            [_final(content="结论：策略正收益。")],
        ]
    )
    runner = AgentRunner(model, registry, ToolResultStore(), ContextBudget())
    session = _session()
    artifacts = runner.run(session=session, user_message="回测一下", system_prompt="SYS", max_steps=3, on_event=lambda event: None)
    assert artifacts["strategy"]["name"] == "AI 生成策略"
    assert artifacts["chart"]["type"] == "equity_curve"
    assert artifacts["chart"]["points"][0]["equity"] == 1_000_000
    # 普通工具调用不产生工件
    plain_registry = _registry()
    plain_model = FakeModel(
        [
            [_final(tool_calls=[_tool_call("t1", "echo_tool", '{"x": 1}')])],
            [_final(content="ok")],
        ]
    )
    plain_runner = AgentRunner(plain_model, plain_registry, ToolResultStore(), ContextBudget())
    plain_artifacts = plain_runner.run(
        session=_session(), user_message="q", system_prompt="SYS", max_steps=2, on_event=lambda event: None
    )
    assert plain_artifacts == {}


def test_agent_budget_digests_long_tool_summaries():
    long_summary = "y" * 5_000
    registry = ToolRegistry()
    registry.register(
        AiTool(
            name="noisy_tool",
            description="noisy",
            parameters={"type": "object", "properties": {}},
            executor=lambda args: {"ok": True},
            summarizer=lambda payload: long_summary,
        )
    )
    model = FakeModel(
        [
            [_final(tool_calls=[_tool_call("t1", "noisy_tool", "{}")])],
            [_final(content="done")],
        ]
    )
    budget = ContextBudget(digest_chars=100)
    runner = AgentRunner(model, registry, ToolResultStore(), budget)
    session = _session()
    runner.run(session=session, user_message="q", system_prompt="SYS", max_steps=2, on_event=lambda event: None)
    tool_message = next(message for message in session["messages"] if message.get("role") == "tool")
    assert len(tool_message["content"]) < 200
