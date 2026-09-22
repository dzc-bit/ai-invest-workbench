"""JSON-file session persistence under 运行产物/AI对话.

Sessions hold protocol messages (tool contents are digests only) plus display
turns for the frontend; full tool payloads stay in the in-memory
:class:`ToolResultStore` and are never persisted.

Format evolution follows **adjacent migration**: a new version may add fields
but never moves, rewrites or destroys generations already on disk, and the read
side keeps tolerating older generations (files predating ``schema_version`` are
treated as v1).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

SESSIONS_DIR_NAME = "AI对话"
SESSION_SCHEMA_VERSION = 1
MAX_PERSISTED_MESSAGES = 400
# 动态自动清理：抽屉不再提供逐条删除按钮（会话多了列表过挤），改由写侧在每次
# save 后按"条数上限 + 保留天数"回收最久未更新的旧会话，且永不动正在使用的会话。
MAX_SESSIONS = 40
SESSION_RETENTION_DAYS = 30
_SESSION_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_session_id(session_id: str) -> str | None:
    cleaned = _SESSION_ID_RE.sub("", str(session_id))
    return cleaned[:64] or None


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse ``updated_at``; ``None`` when unusable (see ``prune`` for the policy)."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _looks_like_session(payload: Any) -> bool:
    """Shape check shared by ``list_sessions`` and ``prune``.

    The sessions directory must not be treated as a generic JSON dump:回收一个
    不是会话的文件（导出、手写笔记）比留着更糟。判定口径与 ``list_sessions``
    一致——有 ``session_id`` 才是会话。
    """
    return isinstance(payload, dict) and bool(payload.get("session_id"))


def sanitize_session_id(session_id: str) -> str | None:
    """Public wrapper used by the facade to key per-session locks."""
    return _safe_session_id(session_id)


class SessionStore:
    def __init__(self, ai_base_dir: str | Path) -> None:
        self._dir = Path(ai_base_dir) / SESSIONS_DIR_NAME
        # 优雅的"正在生成中"判定由 facade 注入（它才持有会话锁表）；未注入时
        # 视为全部空闲，动态清理依旧可用。
        self._busy_check: Callable[[str], bool] | None = None

    @property
    def directory(self) -> Path:
        return self._dir

    def set_busy_check(self, check: Callable[[str], bool] | None) -> None:
        """Register the single source of truth for "this session is working"."""
        self._busy_check = check

    def _is_busy(self, session_id: str) -> bool:
        check = self._busy_check
        if check is None:
            return False
        try:
            return bool(check(session_id))
        except Exception:  # noqa: BLE001 - 清理判定绝不能反过来打断写入
            return True

    def create(self, title: str = "新会话") -> dict:
        now = datetime.now(UTC).isoformat()
        session = {
            "session_id": uuid4().hex,
            "schema_version": SESSION_SCHEMA_VERSION,
            "title": title[:40] or "新会话",
            "created_at": now,
            "updated_at": now,
            "rolling_summary": "",
            "pending_archive": [],
            "messages": [],
            "display": [],
        }
        self.save(session)
        return session

    def get(self, session_id: str) -> dict | None:
        safe = _safe_session_id(session_id)
        if safe is None:
            return None
        path = self._dir / f"{safe}.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        payload.setdefault("schema_version", SESSION_SCHEMA_VERSION)
        return payload

    def save(self, session: dict) -> None:
        safe = _safe_session_id(str(session.get("session_id", "")))
        if safe is None:
            raise ValueError("session_id 不合法")
        session["session_id"] = safe
        session["updated_at"] = datetime.now(UTC).isoformat()
        messages = session.get("messages") or []
        if len(messages) > MAX_PERSISTED_MESSAGES:
            session["messages"] = messages[-MAX_PERSISTED_MESSAGES:]
        display = session.get("display") or []
        if len(display) > MAX_PERSISTED_MESSAGES:
            session["display"] = display[-MAX_PERSISTED_MESSAGES:]
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{safe}.json"
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, path)
        self.prune(exclude=[safe])

    def list_sessions(self) -> list[dict]:
        if not self._dir.exists():
            return []
        items: list[dict] = []
        for path in self._dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and payload.get("session_id"):
                items.append(
                    {
                        "session_id": payload.get("session_id"),
                        "title": payload.get("title", "新会话"),
                        "updated_at": payload.get("updated_at"),
                        "message_count": len(payload.get("display") or []),
                    }
                )
        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return items

    def delete(self, session_id: str) -> bool:
        safe = _safe_session_id(session_id)
        if safe is None:
            return False
        path = self._dir / f"{safe}.json"
        if not path.exists():
            return False
        path.unlink()
        return True

    def prune(
        self,
        *,
        exclude: Iterable[str] = (),
        max_sessions: int = MAX_SESSIONS,
        retention_days: int = SESSION_RETENTION_DAYS,
    ) -> list[str]:
        """Reclaim old sessions so the transcript list stays usable without a
        delete button: drop sessions older than ``retention_days`` and, if more
        than ``max_sessions`` remain, the least recently updated ones.

        ``exclude`` (plus anything the injected busy check reports) is never
        touched — the caller is mid-``save`` on its own file, and a session that
        is still generating would be resurrected by its worker's ``finally``.

        Only files that actually look like sessions are ever removed (same shape
        check as :meth:`list_sessions`): the directory is not a general-purpose
        JSON folder, and a stray export/hand-written file must not be reclaimed
        just because it has no readable ``updated_at``.

        Returns the removed session ids. Failures are swallowed: housekeeping
        must never break a chat turn.
        """
        if not self._dir.exists():
            return []
        keep = {value for value in exclude if value}
        now = datetime.now(UTC)
        dated: list[tuple[str, Path, datetime]] = []
        undated: list[tuple[str, Path]] = []
        for path in self._dir.glob("*.json"):
            session_id = path.stem
            if session_id in keep or self._is_busy(session_id):
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not _looks_like_session(payload):
                continue
            updated = _parse_timestamp(payload.get("updated_at"))
            # updated_at 不可读时无法判断年龄：按"最旧"参与条数回收，但绝不
            # 参与保留期淘汰——读不懂时间戳不是销毁文件的理由。
            if updated is None:
                undated.append((session_id, path))
            else:
                dated.append((session_id, path, updated))
        removed: list[str] = []
        survivors: list[tuple[str, Path, datetime]] = []
        for session_id, path, updated in dated:
            if retention_days > 0 and (now - updated).days >= retention_days:
                if _unlink(path):
                    removed.append(session_id)
                continue
            survivors.append((session_id, path, updated))
        overflow = len(survivors) + len(undated) - max_sessions if max_sessions > 0 else 0
        if overflow > 0:
            # 时间戳不可读的按"最旧"处理，优先回收；再按 updated_at 从旧到新补足。
            for session_id, path in undated[:overflow]:
                if _unlink(path):
                    removed.append(session_id)
            overflow -= min(overflow, len(undated))
            if overflow > 0:
                survivors.sort(key=lambda item: item[2], reverse=True)
                for session_id, path, _ in survivors[max(0, len(survivors) - overflow) :]:
                    if _unlink(path):
                        removed.append(session_id)
        return removed
