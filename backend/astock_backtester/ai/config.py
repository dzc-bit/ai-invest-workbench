"""AI runtime configuration persisted under 运行产物/AI配置.

The file lives inside the user-data directory (never committed); ``api_key``
is never returned to the frontend — only a masked hint is.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

CONFIG_DIR_NAME = "AI配置"
CONFIG_FILE_NAME = "ai-config.json"


SUPPORTED_API_STYLES = ("chat-completions", "responses", "anthropic")
SUPPORTED_RESEARCH_STYLES = ("conservative", "balanced", "aggressive")


def normalize_hhmm(value: str, fallback: str) -> str:
    """Normalize a user-supplied HH:MM local time; invalid input falls back."""
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        return fallback
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return fallback
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return fallback
    return f"{hour:02d}:{minute:02d}"


@dataclass
class AiConfig:
    """OpenAI-compatible provider settings. Empty by default until the user
    fills them in the desktop settings dialog.

    ``api_style`` selects the wire protocol:
    - ``chat-completions``: POST {base}/chat/completions（OpenAI 兼容，默认）;
    - ``responses``: OpenAI Responses API（GPT-5 系等新接口）;
    - ``anthropic``: Anthropic Messages API（{base}/v1/messages）。

    ``research_style`` selects the analyst persona: conservative / balanced /
    aggressive（对应保守/均衡/激进三种研究风格，注入 system prompt）。
    """

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    embedding_model: str = ""
    # embedding 服务的独立入口：留空则跟随主 base_url / api_key。
    # 常见场景：chat 走 DeepSeek，embedding 走 SiliconFlow 等独立供应商。
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    api_style: str = "chat-completions"
    research_style: str = "balanced"
    temperature: float = 0.3
    # 单次回答的输出长度上限。之前只以硬编码常量存在于 anthropic 分支，
    # 另外两种协议干脆不传；被截断的回答因此无从调整。
    max_tokens: int = 4096
    max_steps: int = 8
    insights_enabled: bool = True
    insight_max_per_hour: int = 6
    # 定时任务（本地时间 HH:MM）：收盘复盘报告 / 策略库自动体检。
    report_enabled: bool = False
    report_time: str = "15:30"
    evolution_enabled: bool = False
    evolution_time: str = "16:00"

    def is_configured(self) -> bool:
        return bool(self.base_url.strip() and self.api_key.strip() and self.model.strip())

    def sanitized(self) -> AiConfig:
        cfg = AiConfig(**asdict(self))
        cfg.base_url = cfg.base_url.strip().rstrip("/")
        cfg.api_key = cfg.api_key.strip()
        cfg.model = cfg.model.strip()
        cfg.embedding_model = cfg.embedding_model.strip()
        cfg.embedding_base_url = cfg.embedding_base_url.strip().rstrip("/")
        cfg.embedding_api_key = cfg.embedding_api_key.strip()
        if cfg.api_style not in SUPPORTED_API_STYLES:
            cfg.api_style = "chat-completions"
        if cfg.research_style not in SUPPORTED_RESEARCH_STYLES:
            cfg.research_style = "balanced"
        cfg.temperature = min(max(cfg.temperature, 0.0), 2.0)
        cfg.max_tokens = max(256, min(int(cfg.max_tokens), 32_000))
        cfg.max_steps = max(1, min(int(cfg.max_steps), 16))
        cfg.insight_max_per_hour = max(0, min(int(cfg.insight_max_per_hour), 60))
        cfg.report_time = normalize_hhmm(cfg.report_time, "15:30")
        cfg.evolution_time = normalize_hhmm(cfg.evolution_time, "16:00")
        return cfg

    def embedding_endpoint(self) -> tuple[str, str]:
        """Effective (base_url, api_key) for embedding calls: dedicated values
        when set, otherwise the main chat endpoint."""
        return (
            self.embedding_base_url or self.base_url,
            self.embedding_api_key or self.api_key,
        )


def masked_key(api_key: str) -> str:
    key = api_key.strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}****{key[-4:]}"


class AiConfigStore:
    """Load/save ``ai-config.json`` with atomic writes and key preservation.

    ``save`` treats an empty ``api_key`` as "keep the existing key" so the
    settings dialog never needs to round-trip the secret back to the user.
    """

    def __init__(self, ai_base_dir: str | Path) -> None:
        self._dir = Path(ai_base_dir) / CONFIG_DIR_NAME
        self._path = self._dir / CONFIG_FILE_NAME

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> AiConfig:
        if not self._path.exists():
            return AiConfig()
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return AiConfig()
        if not isinstance(payload, dict):
            return AiConfig()
        known = {key: payload[key] for key in asdict(AiConfig()) if key in payload}
        try:
            return AiConfig(**known).sanitized()
        except (TypeError, ValueError):
            return AiConfig()

    def save(self, config: AiConfig) -> AiConfig:
        current = self.load()
        merged = AiConfig(**asdict(config))
        if not merged.api_key.strip():
            merged.api_key = current.api_key
        if not merged.embedding_api_key.strip():
            merged.embedding_api_key = current.embedding_api_key
        # 与 api_key / embedding_api_key 同一套“留空保持”语义：清空独立 embedding
        # 入口应回落到主配置，而不是把已存的地址静默擦掉。
        if not merged.embedding_base_url.strip():
            merged.embedding_base_url = current.embedding_base_url
        merged = merged.sanitized()
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(asdict(merged), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, self._path)
        return merged

    def masked_view(self) -> dict[str, object]:
        config = self.load().sanitized()
        return {
            "base_url": config.base_url,
            "model": config.model,
            "embedding_model": config.embedding_model,
            "embedding_base_url": config.embedding_base_url,
            "embedding_api_key_masked": masked_key(config.embedding_api_key),
            "api_style": config.api_style,
            "research_style": config.research_style,
            "api_key_masked": masked_key(config.api_key),
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "max_steps": config.max_steps,
            "insights_enabled": config.insights_enabled,
            "insight_max_per_hour": config.insight_max_per_hour,
            "report_enabled": config.report_enabled,
            "report_time": config.report_time,
            "evolution_enabled": config.evolution_enabled,
            "evolution_time": config.evolution_time,
            "configured": config.is_configured(),
        }


def ai_base_dir_from_cache_dir(cache_dir: str | Path) -> Path:
    """Resolve 运行产物 root from the data-warehouse directory.

    The warehouse lives at ``运行产物/本地数据仓`` in the desktop layout, so the
    AI user data (config / chats / embedding cache) sits next to it as sibling
    directories.  Falls back to the cache dir itself when the parent is not
    writable (portable/odd layouts).
    """
    root = Path(cache_dir).resolve()
    candidate = root.parent
    probe = candidate / ".ai-write-probe"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return candidate
    except OSError:
        return root
