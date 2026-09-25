from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from astock_backtester.ai.memory import (
    MemoryRecord,
    MemoryStore,
    plan_memory_ops,
    recall_score,
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


def _record(
    rid: str, content: str, category: str = "fact", *, days_ago: int = 0, hits: int = 0, weight: float = 1.0
) -> MemoryRecord:
    updated = datetime.now(UTC) - timedelta(days=days_ago)
    return MemoryRecord(
        id=rid,
        content=content,
        category=category,
        created_at=updated.isoformat(),
        updated_at=updated.isoformat(),
        hits=hits,
        weight=weight,
    )


def test_recall_score_prefers_weight_hits_and_recency():
    now = datetime.now(UTC)
    assert recall_score(_record("c", "明确强调", weight=3.0, hits=4), now) > recall_score(_record("d", "顺带提及"), now)
    assert recall_score(_record("f", "昨天", days_ago=1), now) > recall_score(_record("e", "一个月前", days_ago=60), now)
    # 同权重下，越新分数越高（14 天半衰期衰减）
    assert recall_score(_record("g", "新", days_ago=0), now) > recall_score(_record("h", "旧", days_ago=30), now)


def test_apply_ops_add_update_delete_and_dedupe(tmp_path):
    store = MemoryStore(tmp_path)
    existing = store.load()
    existing.append(_record("m1", "用户关注 600519", "watchlist"))
    store.save(existing)

    applied = store.apply_ops(
        [
            {"op": "add", "content": "用户偏好低换手策略", "category": "style", "weight": 3},
            {"op": "add", "content": "用户关注   600519", "category": "watchlist"},  # 归一化后重复 → 忽略
            {"op": "update", "id": "m1", "content": "用户重仓关注 600519 与 300750", "weight": 3},
            {"op": "delete", "id": "不存在"},
        ]
    )
    assert applied == 2
    records = {record.id: record for record in store.load()}
    assert len(records) == 2
    assert records["m1"].content.startswith("用户重仓关注")
    assert records["m1"].weight == 3.0
    styled = [record for record in records.values() if record.category == "style"]
    assert styled and styled[0].weight == 3.0


def test_plan_memory_ops_parses_and_validates():
    model = ScriptedModel(
        '[{"op": "add", "content": "用户只做主板", "category": "style", "weight": 3},'
        '{"op": "bogus", "content": "x"},'
        '{"op": "update", "id": "m1", "content": "持仓改为 300750"}]'
    )
    ops = plan_memory_ops(model, "user: 我现在只做主板，持仓换成了宁德时代", [_record("m1", "旧持仓")])
    assert len(ops) == 2
    assert ops[0]["category"] == "style" and ops[0]["weight"] == 3
    assert ops[1]["op"] == "update" and ops[1]["id"] == "m1"
    prompt_text = model.prompts[0][0]["content"]
    assert "id=m1" in prompt_text and "最新对话" in prompt_text


def test_plan_memory_ops_tolerates_garbage_and_empty_dialogue():
    assert plan_memory_ops(ScriptedModel("没有变化"), "user: 你好") == []
    assert plan_memory_ops(ScriptedModel("[broken"), "user: 你好") == []
    assert plan_memory_ops(ScriptedModel("[]"), "") == []


def test_profile_and_facts_context_split(tmp_path):
    store = MemoryStore(tmp_path)
    store.apply_ops(
        [
            {"op": "add", "content": "用户只做低估值大盘股", "category": "risk_preference", "weight": 3},
            {"op": "add", "content": "用户偏好打板龙头", "category": "style", "weight": 3},
            {"op": "add", "content": "关注 600519 / 300750", "category": "watchlist"},
            {"op": "add", "content": "常用地设置：最大持仓 5 只", "category": "strategy"},
        ]
    )
    profile = store.profile_context()
    assert "风险偏好" in profile and "交易风格" in profile
    assert "watchlist" not in profile
    facts = store.facts_context()
    assert "[watchlist]" in facts and "[strategy]" in facts


def test_recall_reports_the_injected_ids(tmp_path):
    """召回必须回报"这次进了上下文的是哪几条"，否则 hit-boost 无从累计。"""
    store = MemoryStore(tmp_path)
    store.apply_ops(
        [
            {"op": "add", "content": "风险偏好：只做低估值", "category": "risk_preference", "weight": 3},
            {"op": "add", "content": "关注 600519", "category": "watchlist"},
        ]
    )
    profile, facts, injected = store.recall()
    assert "风险偏好" in profile
    assert "[watchlist]" in facts
    assert len(injected) == 2
    assert set(injected) == {record.id for record in store.load()}


def test_bump_hits_reinforces_recall_ranking(tmp_path):
    """hits 必须真的落盘并抬高名次：此前它永远是 0，hit-boost 是死代码。"""
    store = MemoryStore(tmp_path)
    store.apply_ops([{"op": "add", "content": "关注 600519", "category": "watchlist"}])
    record_id = store.load()[0].id
    before = recall_score(store.load()[0])

    assert store.bump_hits([record_id]) == 1

    record = next(record for record in store.load() if record.id == record_id)
    assert record.hits == 1
    assert recall_score(record) > before


def test_bump_hits_ignores_unknown_ids(tmp_path):
    store = MemoryStore(tmp_path)
    assert store.bump_hits(["不存在"]) == 0
    assert store.load() == []


def _add_many(store: MemoryStore, contents: list[tuple[str, str, float]]) -> None:
    """apply_ops 每次最多应用 4 条操作（模型一次也只产 ≤4 条），测试要分批喂。"""
    for start in range(0, len(contents), 4):
        store.apply_ops(
            [
                {"op": "add", "content": content, "category": category, "weight": weight}
                for content, category, weight in contents[start : start + 4]
            ]
        )


def test_hit_boost_is_capped_so_new_memories_still_get_injected(tmp_path):
    """hit-boost 封顶：反复注入不能把老记忆永久锁死在 top-N。

    hits 无上限时 injection ⊆ top-N 会形成正反馈——只要进过一次就永远加分，
    实测约 44 轮后新记忆再也挤不进注入集合，"越用越准"退化成"越用越固化"。
    封顶值的口径是"用户明确强调（weight=3）优先于只是被反复看到（weight=1 拉满）"。
    """
    store = MemoryStore(tmp_path)
    _add_many(store, [(f"旧记忆 {index}", "fact", 1) for index in range(12)])
    old_ids = [record.id for record in store.load()]
    assert len(old_ids) == 12

    # 模拟长期使用：老记忆被反复命中
    for _ in range(200):
        store.bump_hits(old_ids)

    store.apply_ops([{"op": "add", "content": "新偏好：只做低估值高股息", "category": "risk_preference", "weight": 3}])
    newest = next(record for record in store.load() if record.content.startswith("新偏好"))

    _profile, _facts, injected = store.recall()
    assert newest.id in injected, "新写的高权重记忆必须能进入注入集合，不能被老记忆的 hits 永久挤掉"

    # weight 仍是主序：新记忆得分必须高于 weight=1 的老记忆（哪怕它们 hits 拉满）
    newest_record = next(record for record in store.load() if record.id == newest.id)
    oldest_record = next(record for record in store.load() if record.id == old_ids[0])
    assert recall_score(newest_record) > recall_score(oldest_record)


def test_recall_excludes_facts_dropped_by_the_char_budget(tmp_path):
    """被字符预算挡住的记录不算"已注入"，否则没进上下文的记忆也会被 hit-boost。"""
    store = MemoryStore(tmp_path)
    # 每条都顶到 160 字上限，10 条必然超出 1400 字预算；前缀保证内容两两不同
    # （apply_ops 会按归一化内容去重）。
    _add_many(store, [((f"{index:02d}" + "很长的记忆内容" * 20)[:160], "fact", 3) for index in range(10)])
    records = store.load()
    assert len(records) == 10

    _profile, facts, injected = store.recall()
    rendered = len([line for line in facts.splitlines() if line.strip()])
    assert 0 < rendered < 10, "本用例需要预算真的裁掉了一部分事实"
    assert len(injected) == rendered, "injected_ids 必须与实际渲染进上下文的事实条数一致"


def test_plan_memory_ops_defaults_to_beijing_date():
    """缺省日期锚点不能是 UTC：提示词里写的是"北京时间"，自相矛盾会让模型算错日期。"""
    model = ScriptedModel("[]")
    plan_memory_ops(model, "user: 今天做了个决定")
    prompt_text = model.prompts[0][0]["content"]
    assert "UTC，未校准" not in prompt_text
    assert "北京时间" in prompt_text


def test_plan_memory_ops_anchors_relative_time_to_a_reference_date():
    model = ScriptedModel("[]")
    plan_memory_ops(model, "user: 我昨天止损了 002402", reference_date="2026-09-22")
    prompt_text = model.prompts[0][0]["content"]
    assert "2026-09-22" in prompt_text
    assert "绝对日期" in prompt_text or "换算成绝对日期" in prompt_text


def test_apply_ops_rejects_market_numbers_for_non_profile_categories(tmp_path):
    """写侧拦截：行情数字不是持久事实——它会被每轮注入 system prompt 当权威，
    而数字第二天就过时（真实记忆文件里出现过
    "纠正超声电子数据：9/22 主力净流出 4.44 亿…"）。拒绝必须计数，不静默。"""
    store = MemoryStore(tmp_path)
    applied = store.apply_ops(
        [
            {"op": "add", "content": "纠正数据：9/22 主力净流出 4.44 亿", "category": "fact"},
            {"op": "add", "content": "600206 今天涨停 3 天了", "category": "watchlist"},
            # profile 类（holding/risk_preference/style）豁免：成本、偏好可含数字
            {"op": "add", "content": "持有 600206，2026-09-22 买入", "category": "holding"},
            # 语境词但无数字：不拦（关注资金流偏好是持久事实）
            {"op": "add", "content": "用户关注 600206 的主力净流入变化", "category": "watchlist"},
        ]
    )
    records = store.load()
    assert len(records) == 2
    categories = {record.category for record in records}
    assert categories == {"holding", "watchlist"}
    assert store.rejected_market_facts == 2
    assert applied == 2


def test_apply_ops_merges_same_symbol_watchlist_records(tmp_path):
    """同标的合并：同 category 且含相同 6 位代码的旧记录走 update 而不是新增——
    真实记忆文件里已有"同标的、不同措辞"的重叠条目。"""
    store = MemoryStore(tmp_path)
    store.apply_ops([{"op": "add", "content": "用户关注 600206", "category": "watchlist"}])
    before = len(store.load())
    applied = store.apply_ops([{"op": "add", "content": "关注 600206 的新高突破", "category": "watchlist"}])
    records = store.load()
    assert applied == 1
    assert len(records) == before, "同标的同类别的记忆应合并，不应新增一条"
    assert "新高突破" in records[0].content


def test_explicit_user_ops_update_and_delete_records(tmp_path):
    store = MemoryStore(tmp_path)
    store.apply_ops([{"op": "add", "content": "用户关注 600206", "category": "watchlist"}])
    record = store.load()[0]

    # 用户显式编辑不做行情数字拦截（编辑权在用户），但内容仍生效
    updated = store.update_record(record.id, content="用户关注 600206 的龙虎榜席位", weight=3.0)
    assert updated.content.endswith("席位")
    assert updated.weight == 3.0

    assert store.delete_record(record.id) is True
    assert store.load() == []
    assert store.delete_record(record.id) is False
