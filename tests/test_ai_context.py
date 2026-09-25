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
    # 无任何安全边界的纯字符流：整段丢弃而不是硬切出前 100 个字符——
    # 旧兜底 cut=limit 会按字符硬切，"close": 12.34 被切成 12. 正是这条路。
    result = truncate_text("a" * 500, 100)
    assert "a" not in result
    assert "后续 500 字符未提供" in result

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
    # 无任何安全边界（无换行/逗号/空格）：整段丢弃，绝不按字符硬切——
    # 旧兜底 "cut = limit" 曾把 "close": 12.34 切成 12.，模型读到格式合法
    # 但数值错误的价格。
    digest = budget.digest("b" * 500)
    assert "已截断" in digest
    assert "b" not in digest
    # 有字段边界（逗号落在 [limit//2, limit) 内）：切点落在逗号处，绝不会
    # 留下半个数字/半个 token
    budget16 = ContextBudget(total_chars=100, digest_chars=16)
    safe = budget16.digest("abcdefghij,rest-of-payload")
    assert safe.startswith("abcdefghij")
    assert not safe.startswith("abcdefghij,r")  # 没有越过逗号再硬切
    assert "已截断" in safe
    messages = [{"role": "user", "content": "x" * 200}]
    assert budget.over_budget(messages) is True
    assert budget.over_budget([{"role": "user", "content": "short"}]) is False


def test_compact_context_payload_labels_data_and_truncates():
    payload = {"values": list(range(1000))}
    rendered = compact_context_payload("backtest_result", payload, ContextBudget())
    assert rendered.startswith("[附带上下文 · backtest_result")
    assert "已截断" in rendered


def test_retain_rows_resume_offset_follows_keep_direction():
    from astock_backtester.ai.context import retain_rows

    rows = [{"i": index} for index in range(30)]

    # head 保留：省略的是后续行，从 offset=kept 续读。
    head = retain_rows(rows, max_rows=10, keep="head")
    assert (head.kept, head.omitted) == (10, 20)
    assert head.resume_offset == 10

    # tail 保留（近 N 日行情）：省略的是**最早的**头部行，resume_offset 必须是 0——
    # 旧实现按 kept 续读只会重读已展示的尾部行，头部行永远看不到。
    tail = retain_rows(rows, max_rows=10, keep="tail")
    assert (tail.kept, tail.omitted) == (10, 20)
    assert tail.resume_offset == 0
    assert "最早的 20 行未展开" in tail.text

    # head_tail：省略的是中段，从 head 处续读。
    both = retain_rows(rows, max_rows=10, keep="head_tail")
    assert both.resume_offset == 5

    # 无省略：offset 越过末尾，read_tool_result 会报"已全部读完"。
    full = retain_rows(rows[:5], max_rows=10)
    assert full.resume_offset == 5


def test_retain_rows_number_cells_keep_precision():
    from astock_backtester.ai.context import retain_rows

    rows = [
        {"symbol": "600519", "volume": 12345678.0, "main_net_inflow": 123456789.25, "close": 12.34, "gap": float("nan")}
    ]
    retained = retain_rows(rows, columns=["symbol", "volume", "main_net_inflow", "close", "gap"])
    # %g 会把大数压成 1.23457e+07 / 1.23457e+08 削掉精度，模型可能误读
    assert "12345678" in retained.text
    assert "123456789.25" in retained.text
    assert "e+" not in retained.text
    assert "|12.34|" in retained.text
    # NaN 单元格按缺失渲染，不能出现 "nan"
    assert "nan" not in retained.text
    assert "--" in retained.text


def test_untrusted_digest_keeps_close_marker_after_budget():
    """先压缩再包围栏：内容超预算时 UNTRUSTED_CLOSE 必须保留在摘要里。

    反序（先 wrap 后 digest）会把闭合标记切掉，注入隔离退化成"只有开标记"。
    """
    summary = "\n".join(f"外部抓取第 {index} 行内容" for index in range(400))  # 远超默认 1200 字
    budget = ContextBudget()
    fenced = wrap_untrusted(budget.digest(summary, 1_200))
    assert fenced.startswith(UNTRUSTED_OPEN)
    assert fenced.rstrip().endswith(UNTRUSTED_CLOSE)
