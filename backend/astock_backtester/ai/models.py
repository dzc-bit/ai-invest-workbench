"""Pydantic contracts for the AI HTTP endpoints (additive to root models)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

AiContextKind = Literal["none", "backtest_result", "strategy", "market_snapshot"]


class AiChatContext(BaseModel):
    """Optional structured payload attached by the frontend (e.g. the current
    backtest result digest). Treated as data, never as instructions."""

    kind: AiContextKind = "none"
    payload: dict[str, Any] = Field(default_factory=dict)


class AiChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None
    context: AiChatContext | None = None


class AiStatusResponse(BaseModel):
    configured: bool
    base_url: str
    model: str
    insights_enabled: bool
    tool_names: list[str] = Field(default_factory=list)
    knowledge_documents: int = 0
    knowledge_chunks: int = 0
    knowledge_ready: bool = False
    memory_count: int = 0


class AiInsightRecord(BaseModel):
    id: str
    created_at: datetime
    level: Literal["info", "warning", "high"] = "info"
    title: str
    digest: str
    related_symbols: list[str] = Field(default_factory=list)
    source: str = "ai-insight"
    disclaimer: str = "AI 生成内容，仅供辅助观察，不构成投资建议"


class AiEventStreamEvent(BaseModel):
    """Envelope for /ai/events/stream payloads (insight / data_fresh / heartbeat)."""

    type: Literal["insight", "data_fresh", "heartbeat"]
    insight: AiInsightRecord | None = None
    module: str | None = None
    timestamp: datetime | None = None


class AiConfigUpdate(BaseModel):
    base_url: str = ""
    model: str = ""
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    api_key: str = ""
    api_style: str = "chat-completions"
    research_style: str = "balanced"
    temperature: float = 0.3
    max_tokens: int = 4096
    max_steps: int = 8
    insights_enabled: bool = True
    insight_max_per_hour: int = 6
    report_enabled: bool = False
    report_time: str = "15:30"
    evolution_enabled: bool = False
    evolution_time: str = "16:00"
