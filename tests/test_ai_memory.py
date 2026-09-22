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
