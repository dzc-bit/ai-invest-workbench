"""Startup AI digest: agent gathers multi-source market events and synthesizes
a structured briefing, stored under 运行产物/AI简报 and exposed via ``GET
/ai/news`` plus the ``latest_market_digest`` agent tool.

Sources are the existing read-only tools (news providers, limit-up pool,
yesterday-limit performance, realtime snapshot, fupan/zaopan) — richer than the
raw news list alone, and every synthesized item is labelled ``ai-agent`` so it
can never be mistaken for a raw market-data module (AGENTS.md module boundary).
Runs automatically once per service start (when an LLM is configured) and then
on a fixed interval; failures are logged and never fatal.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from astock_backtester.ai.context import wrap_untrusted
from astock_backtester.ai.insights import EventBroker
from astock_backtester.ai.prompts import build_digest_messages

DIGEST_DIR_NAME = "AI简报"
DIGEST_FILE_NAME = "digest.json"
MAX_DIGEST_ITEMS = 30
DIGEST_MAX_ITEMS_PER_RUN = 6
DEFAULT_DIGEST_INTERVAL_SECONDS = 3 * 3600
FRESH_THRESHOLD_SECONDS = 30 * 60


@dataclass
class DigestItem:
    id: str
    title: str
    summary: str
    tags: list[str]
    symbols: list[str]
    source: str = "ai-agent"
    created_at: str = ""


class DigestStore:
    def __init__(self, ai_base_dir: str | Path) -> None:
        self._path = Path(ai_base_dir) / DIGEST_DIR_NAME / DIGEST_FILE_NAME
        self._lock = threading.Lock()

    def load(self) -> list[DigestItem]:
        if not self._path.exists():
            return []
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        items = []
        for entry in payload:
            if isinstance(entry, dict) and entry.get("title"):
                try:
                    items.append(DigestItem(**entry))
                except TypeError:
                    continue
        return items

    def save(self, items: list[DigestItem]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps([asdict(item) for item in items], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(tmp_path, self._path)

    def extend(self, new_items: list[DigestItem]) -> list[DigestItem]:
        with self._lock:
            existing = self.load()
            seen = {re.sub(r"\s+", "", item.title) for item in existing}
            for item in new_items:
                key = re.sub(r"\s+", "", item.title)
                if key not in seen:
                    seen.add(key)
                    existing.append(item)
            existing.sort(key=lambda item: item.created_at, reverse=True)
            existing = existing[:MAX_DIGEST_ITEMS]
            self.save(existing)
            return existing

    def latest_timestamp(self) -> str:
        items = self.load()
        return max((item.created_at for item in items), default="")


def _parse_digest_json(content: str) -> list[dict[str, Any]]:
    start = content.find("[")
    end = content.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        payload = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [entry for entry in payload if isinstance(entry, dict) and str(entry.get("title", "")).strip()]


def parse_digest_items(content: str) -> list[DigestItem]:
    now = datetime.now(UTC).isoformat()
    items: list[DigestItem] = []
    for entry in _parse_digest_json(content)[:DIGEST_MAX_ITEMS_PER_RUN]:
        tags = [str(tag)[:12] for tag in (entry.get("tags") or []) if str(tag).strip()][:2]
        symbols = [str(symbol)[:8] for symbol in (entry.get("symbols") or []) if str(symbol).strip()][:5]
        items.append(
            DigestItem(
                id=uuid4().hex[:12],
                title=str(entry.get("title", ""))[:60],
                summary=str(entry.get("summary", ""))[:300],
                tags=tags,
                symbols=symbols,
                created_at=now,
            )
        )
    return items


class DigestEngine:
    """Runs the gather → synthesize → publish pipeline; ``run_once`` is
    synchronous for testability."""

    def __init__(
        self,
        *,
        broker: EventBroker,
        backend: Any,
        model_provider: Any,
        config_provider: Any,
        store: DigestStore,
        interval_seconds: float = DEFAULT_DIGEST_INTERVAL_SECONDS,
    ) -> None:
        self._broker = broker
        self._backend = backend
        self._model_provider = model_provider
        self._config_provider = config_provider
        self._store = store
        self._interval = interval_seconds
        self._lock = threading.Lock()
        # -inf sentinel: time.monotonic() counts from an arbitrary point (boot
        # on Windows), so 0.0 made a fresh engine believe it had "recently run"
        # on any machine with less than FRESH_THRESHOLD_SECONDS of uptime.
        self._last_run = float("-inf")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ai-digest-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # 启动即先跑一次（服务启动触发），之后按固定间隔刷新。
        while True:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - the engine must never crash the service
                try:
                    self._backend.log("warning", f"ai digest engine run failed: {exc}")
                except Exception:  # noqa: BLE001
                    pass
            if self._stop.wait(self._interval):
                break

    # ------------------------------------------------------------------ run
    def run_once(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if not force and time.monotonic() - self._last_run < FRESH_THRESHOLD_SECONDS:
                return {"ok": False, "skipped": "recent_run"}
            config = self._config_provider()
            if not config.is_configured():
                return {"ok": False, "skipped": "not_configured"}
            model = self._model_provider()
            if model is None:
                return {"ok": False, "skipped": "no_model"}
            data_text = self._gather_sources()
            if not data_text:
                return {"ok": False, "skipped": "no_sources"}
            content = ""
            for event in model.chat(build_digest_messages(data_text), tools=None):
                if event[0] == "final":
                    content = str(event[1].get("content") or "")
            parsed = parse_digest_items(content)
            if not parsed:
                return {"ok": False, "skipped": "unparseable"}
            self._last_run = time.monotonic()
            existing_titles = {re.sub(r"\s+", "", item.title) for item in self._store.load()}
            stored = self._store.extend(parsed)
            self._broker.publish({"type": "data_fresh", "module": "ai_news", "timestamp": datetime.now(UTC).isoformat()})
            # 只把“新增”的要点推为快讯；与既有条目同题的不再重复推送。
            fresh = [
                item
                for item in parsed
                if re.sub(r"\s+", "", item.title) not in existing_titles
            ]
            for item in fresh[:2]:
                self._broker.publish(
                    {
                        "type": "insight",
                        "insight": {
                            "id": item.id,
                            "created_at": item.created_at,
                            "level": "info",
                            "title": item.title,
                            "digest": item.summary,
                            "related_symbols": item.symbols,
                            "source": "ai-insight",
                            "disclaimer": "AI 聚合内容，仅供辅助观察，不构成投资建议",
                        },
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
                )
            try:
                self._backend.log("info", f"AI 简报已更新：{len(parsed)} 条要点（累计 {len(stored)} 条）")
            except Exception:  # noqa: BLE001
                pass
            return {"ok": True, "items": len(parsed)}

    def _crawled_block(self, label: str, body: str) -> str:
        # 新闻标题、涨停池、复盘正文都来自上游站点：进模型上下文前必须套
        # 不可信分隔符（AGENTS.md §15-8）——工具路径如此，定时引擎路径同样如此。
        return f"{label}\n{wrap_untrusted(body)}"

    def _gather_sources(self) -> str:
        sections: list[str] = []
        try:
            news = self._backend.news_provider.latest_news()
            headlines = [f"- {item.title}（{item.source}）" for item in news.items[:12]]
            if headlines:
                sections.append(self._crawled_block("【新闻/电报】", "\n".join(headlines)))
        except Exception:  # noqa: BLE001
            pass
        try:
            from astock_backtester.ai.tools.astock_data_tools import fetch_limit_up_rows

            zt = fetch_limit_up_rows("zt")
            if zt:
                lines = [
                    f"- {row['name']}（{row['symbol']}）{row['zt_stat']} 行业:{row['industry']}" for row in zt[:10]
                ]
                sections.append(self._crawled_block(f"【涨停池·共 {len(zt)} 只】", "\n".join(lines)))
            yzt = fetch_limit_up_rows("yzt")
            if yzt:
                avg_change = sum(row["change_pct"] for row in yzt) / len(yzt)
                strongest = max(yzt, key=lambda row: row["change_pct"])
                sections.append(
                    self._crawled_block(
                        "【昨日涨停今日表现】",
                        f"共 {len(yzt)} 只，平均涨幅 {avg_change:+.2f}%，"
                        f"最强 {strongest['name']} {strongest['change_pct']:+.2f}%",
                    )
                )
        except Exception:  # noqa: BLE001
            pass
        try:
            snapshot = self._backend.realtime_provider.market_snapshot()
            breadth = snapshot.breadth
            index_lines = [
                f"- {index.name} {index.last}（{(index.change_pct or 0):+.2f}%）" for index in snapshot.indexes[:4]
            ]
            if breadth is not None:
                index_lines.append(f"- 红盘 {breadth.up} / 全市场 {breadth.total}（{snapshot.status}）")
            if index_lines:
                sections.append(self._crawled_block("【实时行情】", "\n".join(index_lines)))
        except Exception:  # noqa: BLE001
            pass
        try:
            fupan = self._backend.briefing_provider.latest_fupan()
            if fupan.summary:
                sections.append(self._crawled_block("【同花顺复盘】", fupan.summary[:400]))
        except Exception:  # noqa: BLE001
            pass
        return "\n\n".join(sections)

    # ----------------------------------------------------------------- view
    def view(self) -> dict[str, Any]:
        items = self._store.load()
        return {
            "items": [asdict(item) for item in items[:12]],
            "count": len(items),
            "updated_at": self._store.latest_timestamp() or None,
        }
