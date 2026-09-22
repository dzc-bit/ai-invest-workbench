from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from astock_backtester.ai.sessions import SESSION_SCHEMA_VERSION, SessionStore


def _iso_days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _backdate(store: SessionStore, session_id: str, updated_at: str) -> None:
    """改写落盘时间戳：``save`` 每次都会刷新 updated_at，测清理必须直接动文件。"""
    path = store.directory / f"{session_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["updated_at"] = updated_at
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_session(
    store: SessionStore, session_id: str, *, updated_at: str | None, title: str = "会话"
) -> str:
    """直接落盘一条会话（绕过 save 的自动清理），让 prune 语义成为被测目标。"""
    store.directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "schema_version": SESSION_SCHEMA_VERSION,
        "title": title,
        "created_at": updated_at,
        "messages": [],
        "display": [{"role": "user", "content": title}],
    }
    if updated_at is not None:
        payload["updated_at"] = updated_at
    (store.directory / f"{session_id}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return session_id



def test_create_get_save_roundtrip(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create(title="测试会话")
    assert session["messages"] == [] and session["display"] == []

    session["messages"].append({"role": "user", "content": "你好"})
    session["display"].append({"role": "user", "content": "你好"})
    store.save(session)

    loaded = store.get(session["session_id"])
    assert loaded is not None
    assert loaded["title"] == "测试会话"
    assert loaded["messages"][0]["content"] == "你好"


def test_old_generation_without_schema_version_still_reads_back(tmp_path):
    """相邻迁移的读侧兼容：schema_version 之前落盘的会话必须能继续读。"""
    store = SessionStore(tmp_path)
    session = store.create("旧代会话")
    path = store.directory / f"{session['session_id']}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("schema_version")
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded = store.get(session["session_id"])
    assert loaded is not None
    assert loaded["schema_version"] == SESSION_SCHEMA_VERSION
    assert loaded["title"] == "旧代会话"


def test_new_generation_may_add_fields_and_old_fields_survive(tmp_path):
    """相邻迁移的写侧规则：新版本可以加字段，绝不移动/改写已落盘的会话代。"""
    store = SessionStore(tmp_path)
    session = store.create("加字段会话")
    session["future_field"] = {"reserved": True}
    session["rolling_summary"] = "历史纪要"
    store.save(session)

    loaded = store.get(session["session_id"])
    assert loaded is not None
    assert loaded["future_field"] == {"reserved": True}
    assert loaded["rolling_summary"] == "历史纪要"
    assert loaded["schema_version"] == SESSION_SCHEMA_VERSION
    assert store.list_sessions()[0]["session_id"] == session["session_id"]


def test_delete_session_refuses_while_turn_is_generating(tmp_path):
    """worker 在 finally 里会把会话重新写回；生成中删除会复活文件，必须拒绝。"""
    from astock_backtester.ai.errors import AiSessionBusy
    from astock_backtester.ai.facade import AiService, _SessionLockEntry
    from astock_backtester.ai.sessions import sanitize_session_id

    service = AiService(
        cache_dir=str(tmp_path / "本地数据仓"),
        backend=SimpleNamespace(),
        log=lambda *args, **kwargs: None,
    )
    session = service._sessions.create("生成中的会话")
    safe = sanitize_session_id(session["session_id"])
    assert safe is not None
    with service._session_locks_guard:
        entry = service._session_locks.setdefault(safe, _SessionLockEntry())
    with entry.lock:  # 模拟 worker 正在生成
        with pytest.raises(AiSessionBusy):
            service.delete_session(session["session_id"])
    # 生成结束（锁释放）后即可正常删除
    assert service.delete_session(session["session_id"]) is True
    assert service._sessions.get(session["session_id"]) is None


def test_session_busy_covers_the_claimed_but_not_yet_acquired_window(tmp_path):
    """"已认领、还没拿到锁"的窗口必须仍判为忙。

    ``chat_stream`` 先 ``retain()``（refs+1）再 ``acquire()``；只看 ``lock.locked()``
    会把这个窗口判成空闲，而动态清理每轮 save 都跑——被误删的正是在跑的会话，
    worker 收尾时又把它写回来（列表闪一下又出现），失败时那一轮直接丢。
    """
    from astock_backtester.ai.facade import AiService
    from astock_backtester.ai.sessions import sanitize_session_id

    service = AiService(
        cache_dir=str(tmp_path / "本地数据仓"),
        backend=SimpleNamespace(),
        log=lambda *args, **kwargs: None,
    )
    session = service._sessions.create("即将开跑的会话")
    safe = sanitize_session_id(session["session_id"])
    assert safe is not None

    # 模拟 chat_stream 的"已 retain、尚未 acquire"瞬间：refs>0 且锁空闲
    entry = service._session_lock(safe)
    assert entry.lock.locked() is False
    assert service._session_busy(safe) is True, "认领窗口内必须判为忙，否则清理会删掉正在跑的会话"

    # 开跑后（持锁）同样忙
    assert entry.acquire(timeout=1)
    assert service._session_busy(safe) is True
    entry.release()
    service._session_locks_guard.acquire()
    entry.refs = 0
    service._session_locks_guard.release()
    assert service._session_busy(safe) is False


def test_prune_never_removes_a_session_in_the_claimed_window(tmp_path):
    """prune 必须尊重同一个判定：认领窗口内不准回收。"""
    from astock_backtester.ai.facade import AiService
    from astock_backtester.ai.sessions import sanitize_session_id

    service = AiService(
        cache_dir=str(tmp_path / "本地数据仓"),
        backend=SimpleNamespace(),
        log=lambda *args, **kwargs: None,
    )
    session = service._sessions.create("即将开跑")
    safe = sanitize_session_id(session["session_id"])
    assert safe is not None
    _backdate(service._sessions, safe, _iso_days_ago(90))

    entry = service._session_lock(safe)  # refs>0，锁仍空闲
    try:
        assert service._sessions.prune(max_sessions=0, retention_days=30) == []
        assert service._sessions.get(safe) is not None
    finally:
        service._session_locks_guard.acquire()
        entry.refs = max(0, entry.refs - 1)
        service._session_locks_guard.release()


def test_list_and_delete(tmp_path):
    store = SessionStore(tmp_path)
    first = store.create("第一条")
    second = store.create("第二条")
    assert {item["title"] for item in store.list_sessions()} == {"第一条", "第二条"}
    assert store.delete(first["session_id"]) is True
    assert store.get(first["session_id"]) is None
    assert [item["session_id"] for item in store.list_sessions()] == [second["session_id"]]


def test_unsafe_session_id_rejected(tmp_path):
    store = SessionStore(tmp_path)
    assert store.get("../../etc/passwd") is None
    assert store.delete("bad/id") is False
    # 斜杠等非法字符被清洗而不是穿透路径
    session = {"session_id": "bad/id", "display": [], "messages": []}
    store.save(session)
    assert (tmp_path / "AI对话" / "badid.json").exists()
    # 清洗后为空则拒绝
    with pytest.raises(ValueError):
        store.save({"session_id": "///", "display": [], "messages": []})


def test_persisted_messages_capped(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    for index in range(500):
        session["messages"].append({"role": "user", "content": str(index)})
        session["display"].append({"role": "user", "content": str(index)})
    store.save(session)
    loaded = store.get(session["session_id"])
    assert len(loaded["messages"]) == 400
    assert len(loaded["display"]) == 400


def test_save_prunes_to_max_sessions_keeping_newest(tmp_path):
    """抽屉没有删除按钮后，会话回收全靠 save 后的动态清理。

    ``exclude`` 是"调用方自己的那条会话"，不参与上限计数也不被淘汰，
    所以盘上最终是 ``max_sessions`` 条旧会话 + 1 条正在用的会话。
    """
    store = SessionStore(tmp_path)
    ids = [_write_session(store, f"session-{index}", updated_at=_iso_days_ago(8 - index)) for index in range(8)]

    keep = _write_session(store, "session-current", updated_at=_iso_days_ago(0))
    store.prune(exclude=[keep], max_sessions=3, retention_days=0)

    survivors = {item["session_id"] for item in store.list_sessions()}
    assert survivors == {keep, *ids[-3:]}


def test_save_automatically_prunes_old_sessions(tmp_path):
    """写侧自动回收：一次 save 之后过老的会话就已经不在盘上了（无需 UI 删除入口）。"""
    store = SessionStore(tmp_path)
    _write_session(store, "session-stale", updated_at=_iso_days_ago(90))

    fresh = store.create("刚刚")
    store.save(fresh)

    assert store.get("session-stale") is None
    assert store.get(fresh["session_id"]) is not None


def test_prune_drops_sessions_past_retention_even_under_the_count_cap(tmp_path):
    store = SessionStore(tmp_path)
    stale = _write_session(store, "session-stale", updated_at=_iso_days_ago(90))
    fresh = _write_session(store, "session-fresh", updated_at=_iso_days_ago(1))

    removed = store.prune(max_sessions=0, retention_days=30)
    assert removed == [stale]
    assert store.get(stale) is None
    assert store.get(fresh) is not None


def test_prune_never_touches_excluded_or_busy_sessions(tmp_path):
    """正在生成中的会话被 prune 掉，会被 worker 的 finally 重新写回来。"""
    store = SessionStore(tmp_path)
    busy = _write_session(store, "session-busy", updated_at=_iso_days_ago(90))
    current = _write_session(store, "session-current", updated_at=_iso_days_ago(90))
    store.set_busy_check(lambda session_id: session_id == busy)

    removed = store.prune(exclude=[current], max_sessions=0, retention_days=30)
    assert removed == []
    assert store.get(busy) is not None
    assert store.get(current) is not None


def test_prune_keeps_unreadable_timestamps_unless_count_cap_forces_it(tmp_path):
    """读不懂 updated_at 不是销毁文件的理由：保留期淘汰不适用，条数回收才适用。"""
    store = SessionStore(tmp_path)
    broken = _write_session(store, "session-broken", updated_at=None)
    keep = _write_session(store, "session-keep", updated_at=_iso_days_ago(1))

    # 保留期只针对可读时间戳：坏时间的会话必须活下来
    assert store.prune(max_sessions=0, retention_days=30) == []
    assert store.get(broken) is not None
    # 条数上限需要回收时，坏时间的排在最旧一档被淘汰
    assert store.prune(max_sessions=1, retention_days=0) == [broken]
    assert store.get(keep) is not None
