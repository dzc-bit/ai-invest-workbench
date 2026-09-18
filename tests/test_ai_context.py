from __future__ import annotations

from astock_backtester.ai.context import (
    UNTRUSTED_CLOSE,
    UNTRUSTED_OPEN,
    ContextBudget,
    ToolResult,
    ToolResultStore,
    compact_context_payload,
    truncate_text,
    wrap_untrusted,
)


def test_truncate_text_marks_dropped_chars():
    text = "a" * 500
    result = truncate_text(text, 100)
    assert result.startswith("a" * 100)
    assert "后续 400 字符未提供" in result

    # 截断只能落在行边界：把"收盘价 12.34"切成"收盘价 12."会让模型读到格式
    # 合法但数值错误的价格，比整行丢弃危险得多。
    lines = truncate_text("\n".join(["收盘价 12.34"] * 30), 100)
    kept = lines.split("\n...[已截断")[0]
    assert all(line == "收盘价 12.34" for line in kept.splitlines())


def test_truncate_text_keeps_short_text():
    assert truncate_text("abc", 100) == "abc"


def test_wrap_untrusted_adds_delimiters_and_truncates():
    wrapped = wrap_untrusted("x" * 10_000)
    assert wrapped.startswith(UNTRUSTED_OPEN)
    assert wrapped.endswith(UNTRUSTED_CLOSE)
    assert len(wrapped) < 6_000


def test_tool_result_store_evicts_oldest():
    store = ToolResultStore(max_entries=2)
    for index in range(3):
        store.put(ToolResult(call_id=f"c{index}", name="tool", arguments={}, payload={}, summary="s"))
    assert store.get("c0") is None
    assert store.get("c2") is not None
    assert len(store) == 2


def test_budget_digest_and_over_budget():
    budget = ContextBudget(total_chars=100, digest_chars=10)
    digest = budget.digest("b" * 500)
    assert digest.startswith("b" * 10)
    assert "已截断" in digest
    messages = [{"role": "user", "content": "x" * 200}]
    assert budget.over_budget(messages) is True
    assert budget.over_budget([{"role": "user", "content": "short"}]) is False


def test_compact_context_payload_labels_data_and_truncates():
    payload = {"values": list(range(1000))}
    rendered = compact_context_payload("backtest_result", payload, ContextBudget())
    assert rendered.startswith("[附带上下文 · backtest_result")
    assert "已截断" in rendered
