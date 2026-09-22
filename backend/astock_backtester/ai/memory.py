"""Long-term memory for the AI assistant — "the more you use it, the better it knows you".

Layered design (MemGPT/Letta tiering + mem0-style consolidation), fully local:

- Short term: the agent's protocol-message window is capped (10 messages);
  overflow is folded into the session rolling summary (see ``AgentRunner``).
- Long term: after each turn a single LLM call plans **operations** against the
  existing memory store (add / update / delete), so memories are consolidated
  instead of blindly appended.  Records carry a category and a weight; recall
  ranks by ``weight × hit-boost × recency-decay`` so important, recently
  confirmed facts surface first and stale ones sink.
- Recall splits into a **user profile** (risk preference / style / holdings —
  the "knows you" part) and ranked facts, both injected into the system prompt.

Memory failures are always non-fatal.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

MEMORY_DIR_NAME = "AI记忆"
MEMORY_FILE_NAME = "memory.json"
MAX_RECORDS = 200
MAX_CONTENT_CHARS = 160
PROFILE_LIMIT = 5
FACTS_LIMIT = 10
FACTS_BUDGET_CHARS = 1_400

CATEGORIES = ("risk_preference", "watchlist", "holding", "style", "strategy", "fact")
PROFILE_CATEGORIES = ("risk_preference", "style", "holding")
VALID_OPS = ("add", "update", "delete")

MEMORY_OPS_PROMPT = """你是用户记忆管理员。根据“最新对话”维护用户的长期记忆（记忆只关于用户本人的持久事实，不存行情数字）。
现有记忆：
{existing}

最新对话：
{dialogue}

规则：
- 只产出 ≤4 条操作；与现有记忆重复或矛盾时用 update（带上原 id）修正，过时/作废的用 delete。
- add/update 的 content ≤60 字；category 从 risk_preference(风险偏好)/watchlist(关注标的)/holding(持仓)\
/style(交易风格)/strategy(常用策略参数)/fact(其他事实) 中选。
- weight 1-3：3=用户明确强调（如“我只做低估值”），2=明确陈述，1=顺带提及。
- 用户表明风险偏好、交易风格、加减仓习惯的变化时必须更新。
- 没有值得记忆的变化就输出 []。
- 只输出 JSON 数组，格式：\
[{{"op": "add", "content": "...", "category": "watchlist", "weight": 2}}, \
{{"op": "update", "id": "...", "content": "..."}}, {{"op": "delete", "id": "..."}}]"""


@dataclass
class MemoryRecord:
    id: str
    content: str
    category: str
    created_at: str
    updated_at: str
    hits: int = 0
    weight: float = 1.0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize(content: str) -> str:
    return re.sub(r"\s+", "", content)[:MAX_CONTENT_CHARS]


def recall_score(record: MemoryRecord, now: datetime | None = None) -> float:
    """weight × hit-boost × recency-decay (14 天半衰期)."""
    now = now or datetime.now(UTC)
    try:
        updated = datetime.fromisoformat(record.updated_at)
    except ValueError:
        updated = now
    age_days = max(0.0, (now - updated).total_seconds() / 86_400)
    decay = math.exp(-age_days / 14.0)
    return float(record.weight) * (1.0 + 0.15 * record.hits) * (0.2 + 0.8 * decay)


class MemoryStore:
    def __init__(self, ai_base_dir: str | Path) -> None:
        self._path = Path(ai_base_dir) / MEMORY_DIR_NAME / MEMORY_FILE_NAME
        self._lock = threading.Lock()

    def load(self) -> list[MemoryRecord]:
        if not self._path.exists():
            return []
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        records = []
        for item in payload:
            if isinstance(item, dict) and item.get("id") and item.get("content"):
                try:
                    records.append(MemoryRecord(**item))
                except TypeError:
                    continue
        return records

    def save(self, records: list[MemoryRecord]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps([asdict(record) for record in records], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(tmp_path, self._path)

    # ------------------------------------------------------------------ ops
    def apply_ops(self, ops: list[dict[str, Any]]) -> int:
        """Apply add/update/delete operations; returns applied count."""
        applied = 0
        with self._lock:
            records = self.load()
            for op in ops[:4]:
                kind = str(op.get("op", "")).lower()
                content = str(op.get("content", "")).strip()[:MAX_CONTENT_CHARS]
                if kind == "add" and content:
                    normalized = _normalize(content)
                    if any(_normalize(record.content) == normalized for record in records):
                        continue
                    category = str(op.get("category", "fact"))
                    if category not in CATEGORIES:
                        category = "fact"
                    try:
                        weight = min(3.0, max(1.0, float(op.get("weight", 1.0))))
                    except (TypeError, ValueError):
                        weight = 1.0
                    now = _now_iso()
                    records.append(
                        MemoryRecord(
                            id=uuid4().hex[:12],
                            content=content,
                            category=category,
                            created_at=now,
                            updated_at=now,
                            weight=weight,
                        )
                    )
                    applied += 1
                elif kind == "update" and content:
                    target = next((record for record in records if record.id == str(op.get("id", ""))), None)
                    if target is None:
                        continue
                    target.content = content
                    if str(op.get("category", "")) in CATEGORIES:
                        target.category = str(op["category"])
                    if op.get("weight") is not None:
                        try:
                            target.weight = min(3.0, max(1.0, float(op["weight"])))
                        except (TypeError, ValueError):
                            pass
                    target.updated_at = _now_iso()
                    applied += 1
                elif kind == "delete":
                    before = len(records)
                    records = [record for record in records if record.id != str(op.get("id", ""))]
                    if len(records) < before:
                        applied += 1
            records.sort(key=lambda item: item.updated_at, reverse=True)
            self.save(records[:MAX_RECORDS])
        return applied

    # --------------------------------------------------------------- recall
    def _ranked(self) -> list[MemoryRecord]:
        return sorted(self.load(), key=lambda record: recall_score(record), reverse=True)

    def profile_context(self) -> str:
        """用户画像：风险偏好 / 交易风格 / 持仓——最"懂你"的部分。"""
        records = [record for record in self._ranked() if record.category in PROFILE_CATEGORIES]
        lines = []
        labels = {"risk_preference": "风险偏好", "style": "交易风格", "holding": "持仓/关注"}
        for record in records[:PROFILE_LIMIT]:
            lines.append(f"- {labels.get(record.category, record.category)}：{record.content[:MAX_CONTENT_CHARS]}")
        return "\n".join(lines)

    def facts_context(self) -> str:
        facts = [record for record in self._ranked() if record.category not in PROFILE_CATEGORIES]
        lines = []
        for record in facts[:FACTS_LIMIT]:
            lines.append(f"- [{record.category}] {record.content[:MAX_CONTENT_CHARS]}")
        return "\n".join(lines)[:FACTS_BUDGET_CHARS]

    def count(self) -> int:
        return len(self.load())


def plan_memory_ops(model: Any, dialogue_text: str, existing: list[MemoryRecord] | None = None) -> list[dict[str, Any]]:
    """One LLM call decides how the memory store should change (mem0-style)."""
    if not dialogue_text.strip():
        return []
    existing_lines = [
        f"id={record.id} [{record.category}] {record.content[:MAX_CONTENT_CHARS]}"
        for record in (existing or [])[:20]
    ]
    prompt = MEMORY_OPS_PROMPT.format(
        existing="\n".join(existing_lines) if existing_lines else "（空）",
        dialogue=dialogue_text[:3_000],
    )
    final_content = ""
    for event in model.chat([{"role": "user", "content": prompt}], tools=None):
        if event[0] == "final":
            final_content = str(event[1].get("content") or "")
    start = final_content.find("[")
    end = final_content.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        payload = json.loads(final_content[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    ops: list[dict[str, Any]] = []
    for item in payload[:4]:
        if not isinstance(item, dict) or str(item.get("op", "")).lower() not in VALID_OPS:
            continue
        op: dict[str, Any] = {
            "op": str(item["op"]).lower(),
            "id": str(item.get("id", "")),
            "content": str(item.get("content", ""))[:MAX_CONTENT_CHARS],
            "category": str(item.get("category", "fact")),
            "weight": item.get("weight", 1.0),
        }
        if op["content"].strip() or op["op"] == "delete":
            ops.append(op)
    return ops
