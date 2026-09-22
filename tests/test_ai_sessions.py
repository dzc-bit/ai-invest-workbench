from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from astock_backtester.ai.sessions import SESSION_SCHEMA_VERSION, SessionStore


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
