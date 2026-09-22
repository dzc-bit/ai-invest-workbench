"""One-shot AI commentary: scene + context in, a single paragraph out.

``POST /ai/insight/oneshot`` funnels every "AI 点评一下" button (results
overview, data-center coverage, risk alerts) through this module so the
service never grows per-scene streaming endpoints. Failures raise ``AiError``
subclasses with stable codes; the frontend decides to stay silent.
"""

from __future__ import annotations

import json
from typing import Any

from astock_backtester.ai.prompts import build_oneshot_messages

ONESHOT_SCENES = ("results_overview", "data_coverage", "risk_alerts")
MAX_CONTEXT_CHARS = 3000
MAX_OUTPUT_CHARS = 600


def oneshot_text(model: Any, messages: list[dict[str, str]]) -> str:
    """Run one non-tool chat call and return the final text content."""
    content = ""
    for event in model.chat(messages, tools=None):
        if event[0] == "final":
            content = str((event[1] or {}).get("content") or "")
    return content.strip()[:MAX_OUTPUT_CHARS]


def compact_context(context: Any) -> str:
    """Serialize the caller-provided context into a bounded prompt fragment."""
    try:
        text = json.dumps(context, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(context)
    return text[:MAX_CONTEXT_CHARS]


def insight_oneshot(model: Any, scene: str, context: Any) -> str:
    if scene not in ONESHOT_SCENES:
        raise ValueError(f"未知点评场景：{scene}（可选：{', '.join(ONESHOT_SCENES)}）")
    text = oneshot_text(model, build_oneshot_messages(scene, compact_context(context)))
    if not text:
        raise ValueError("模型没有返回点评内容")
    return text
