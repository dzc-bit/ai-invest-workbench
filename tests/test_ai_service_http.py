from __future__ import annotations

import json
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener

from astock_backtester.service import create_server

# Loopback traffic must never be routed through developer/system proxies.
_OPENER = build_opener(ProxyHandler({}))


def _request_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with _OPENER.open(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _request_json_allow_error(method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        status = getattr(exc, "code", 500)
        body = getattr(exc, "read", None)
        if callable(body):
            return int(status), json.loads(body().decode("utf-8"))
        raise


def _request_ndjson(url: str, payload: dict) -> list[dict]:
    data = json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    with _OPENER.open(request, timeout=10) as response:
        return [json.loads(line) for line in response.read().decode("utf-8").splitlines() if line.strip()]


def _start_server(tmp_path):
    # Use a warehouse-style subdirectory so AI user data (config/sessions) lands
    # inside tmp_path itself, never in the shared pytest root.
    warehouse = tmp_path / "本地数据仓"
    warehouse.mkdir(exist_ok=True)
    server = create_server(host="127.0.0.1", port=0, cache_dir=warehouse)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    return server, thread, server.server_address[1]


def test_ai_status_reports_unconfigured_by_default(tmp_path):
    server, thread, port = _start_server(tmp_path)
    try:
        status = _request_json("GET", f"http://127.0.0.1:{port}/ai/status")
        assert status["configured"] is False
        assert status["base_url"] == ""
        assert status["knowledge_documents"] >= 3
        assert len(status["tool_names"]) >= 8
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_ai_config_roundtrip_masks_key(tmp_path):
    server, thread, port = _start_server(tmp_path)
    try:
        status, saved = _request_json_allow_error(
            "POST",
            f"http://127.0.0.1:{port}/ai/config",
            {"base_url": "https://api.example.com/v1", "api_key": "sk-secret-123456", "model": "demo"},
        )
        assert status == 200
        assert saved["configured"] is True
        assert "sk-secret-123456" not in json.dumps(saved)
        view = _request_json("GET", f"http://127.0.0.1:{port}/ai/config")
        assert view["api_key_masked"].endswith("3456")
        assert view["configured"] is True

        # empty key keeps existing secret
        updated = _request_json("POST", f"http://127.0.0.1:{port}/ai/config", {"base_url": "https://api2.example.com/v1", "model": "m2"})
        assert updated["configured"] is True
        status_payload = _request_json("GET", f"http://127.0.0.1:{port}/ai/status")
        assert status_payload["configured"] is True
        assert status_payload["base_url"] == "https://api2.example.com/v1"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_ai_config_embedding_endpoint_and_schedule_roundtrip(tmp_path):
    """embedding 独立供应商配置（问题3）与定时任务配置（问题8）的回环。"""
    server, thread, port = _start_server(tmp_path)
    try:
        _request_json(
            "POST",
            f"http://127.0.0.1:{port}/ai/config",
            {
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-chat-key-123456",
                "model": "demo",
                "embedding_base_url": "https://api.siliconflow.cn/v1",
                "embedding_api_key": "sk-embed-key-654321",
                "report_enabled": True,
                "report_time": "9:05",
                "evolution_enabled": True,
                "evolution_time": "16:00",
            },
        )
        view = _request_json("GET", f"http://127.0.0.1:{port}/ai/config")
        assert view["embedding_base_url"] == "https://api.siliconflow.cn/v1"
        assert view["embedding_api_key_masked"].endswith("4321")
        # 掩码视图不泄漏完整 key
        assert "sk-embed-key-654321" not in json.dumps(view)
        assert "sk-chat-key-123456" not in json.dumps(view)
        assert view["report_enabled"] is True
        assert view["report_time"] == "09:05"
        assert view["evolution_time"] == "16:00"
        # 空的 embedding key 保持已有值
        updated = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/ai/config",
            {"base_url": "https://api.deepseek.com/v1", "model": "demo"},
        )
        assert updated["embedding_api_key_masked"].endswith("4321")
        assert updated["embedding_base_url"] == "https://api.siliconflow.cn/v1"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_ai_reports_list_and_file_roundtrip(tmp_path):
    server, thread, port = _start_server(tmp_path)
    try:
        empty = _request_json("GET", f"http://127.0.0.1:{port}/ai/reports")
        assert empty == {"items": []}
        service = server.state.ai_service()
        service._report_store.save("复盘报告-test.md", "# 正文")
        listing = _request_json("GET", f"http://127.0.0.1:{port}/ai/reports")
        assert [item["name"] for item in listing["items"]] == ["复盘报告-test.md"]
        payload = _request_json("GET", f"http://127.0.0.1:{port}/ai/report/file?name=%E5%A4%8D%E7%9B%98%E6%8A%A5%E5%91%8A-test.md")
        assert payload["content"] == "# 正文"
        # 路径穿越/不存在 → 稳定错误码（GET 路由兜底 404，不再打挂连接）
        status, error = _request_json_allow_error("GET", f"http://127.0.0.1:{port}/ai/report/file?name=..%2Fescape.md")
        assert status == 404
        assert error["code"]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_ai_overfit_check_endpoint(tmp_path):
    server, thread, port = _start_server(tmp_path)
    try:
        result = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/ai/overfit/check",
            {"metrics": {"trade_count": 2, "total_return_pct": 0.05, "win_rate_pct": 1.0, "max_drawdown_pct": -0.01}},
        )
        assert result["level"] == "critical"
        assert any(finding["code"] == "few_trades" for finding in result["findings"])
        status, error = _request_json_allow_error("POST", f"http://127.0.0.1:{port}/ai/overfit/check", {})
        assert status == 400
        assert error["code"] == "validation_error"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_ai_config_reveal_returns_full_key(tmp_path):
    server, thread, port = _start_server(tmp_path)
    try:
        _request_json(
            "POST",
            f"http://127.0.0.1:{port}/ai/config",
            {"base_url": "https://api.example.com/v1", "api_key": "sk-reveal-me-9876", "model": "demo"},
        )
        revealed = _request_json("GET", f"http://127.0.0.1:{port}/ai/config/reveal")
        assert revealed == {"api_key": "sk-reveal-me-9876"}
        status = _request_json("GET", f"http://127.0.0.1:{port}/ai/status")
        assert status["memory_count"] == 0
        assert len(status["tool_names"]) >= 11  # 本地 + a-stock-data + 查询/写 + 知识检索
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_ai_chat_stream_without_config_returns_error_event(tmp_path):
    server, thread, port = _start_server(tmp_path)
    try:
        events = _request_ndjson(
            f"http://127.0.0.1:{port}/ai/chat/stream",
            {"message": "帮我看看 600519"},
        )
        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert events[0]["code"] == "ai_not_configured"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_ai_chat_stream_with_stubbed_model(tmp_path, monkeypatch):
    server, thread, port = _start_server(tmp_path)
    _request_json(
        "POST",
        f"http://127.0.0.1:{port}/ai/config",
        {"base_url": "http://127.0.0.1:9", "api_key": "sk-test", "model": "demo"},
    )
    scripted_events = [
        {"type": "phase", "phase": "思考中（第 1/8 步）"},
        {"type": "token", "text": "本地数据正常。"},
    ]

    class StubAgent:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def run(self, *, session, user_message, system_prompt, max_steps, context=None, on_event):
            for event in scripted_events:
                on_event(event)
            session["display"].append({"role": "assistant", "content": "本地数据正常。", "tool_steps": [], "ts": "now"})
            return {}

    ai_service = server.state.ai_service()  # 先实例化，再替换实例上的 agent
    monkeypatch.setattr(ai_service, "_agent", StubAgent())
    try:
        events = _request_ndjson(f"http://127.0.0.1:{port}/ai/chat/stream", {"message": "行情如何"})
        types = [event["type"] for event in events]
        assert types[0] == "session"
        assert "phase" in types and "token" in types
        assert types[-1] == "result"
        result = events[-1]
        assert result["display"][-1]["content"] == "本地数据正常。"
        assert result["session_id"]
    finally:
        server.shutdown()
        thread.join(timeout=5)


class _RecordingStubAgent:
    """按真实 agent 的方式写会话，并记录每一轮"开始前看到多少历史"。

    续用历史的关键证据就是这些计数：只有 session_id 被正确回读并传回
    /ai/chat/stream，第二轮的 agent 才会看到第一轮的协议消息与展示轮次。
    """

    def __init__(self) -> None:
        self.turns: list[dict[str, int]] = []

    def run(self, *, session, user_message, system_prompt, max_steps, context=None, on_event):
        self.turns.append(
            {
                "protocol": len(session.get("messages") or []),
                "display": len(session.get("display") or []),
            }
        )
        session["messages"].append({"role": "user", "content": user_message})
        session["display"].append({"role": "user", "content": user_message, "ts": "now"})
        session["display"].append({"role": "assistant", "content": f"已收到：{user_message}", "tool_steps": [], "ts": "now"})
        return {}


class _SystemPromptStubAgent:
    """只记录本轮拿到的 system prompt，用于守卫风格/记忆的注入口径。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run(self, *, session, user_message, system_prompt, max_steps, context=None, on_event):
        self.prompts.append(system_prompt)
        session["display"].append({"role": "assistant", "content": "已记录。", "tool_steps": [], "ts": "now"})
        return {}


def _seed_memory(server) -> None:
    """写一条"自认龙头选手"的长期记忆：这正是会把三种风格揉平的典型内容。"""
    store = server.state.ai_service()._memory
    store.apply_ops([{"op": "add", "content": "自认是龙头选手，偏好连板妖股", "category": "style", "weight": 3}])


def test_ai_chat_stream_states_style_beats_long_term_memory(tmp_path, monkeypatch):
    """风格必须压过记忆里自述的交易风格，否则切风格等于没切。"""
    server, thread, port = _start_server(tmp_path)
    _request_json(
        "POST",
        f"http://127.0.0.1:{port}/ai/config",
        {"base_url": "http://127.0.0.1:9", "api_key": "sk-test", "model": "demo", "research_style": "conservative"},
    )
    stub = _SystemPromptStubAgent()
    ai_service = server.state.ai_service()
    monkeypatch.setattr(ai_service, "_agent", stub)
    _seed_memory(server)
    try:
        _request_ndjson(f"http://127.0.0.1:{port}/ai/chat/stream", {"message": "帮我看看 002402"})
        prompt = stub.prompts[-1]
        assert "当前研究风格：保守" in prompt
        # 记忆仍要注入（个性化），但必须明确风格优先
        assert "风格与记忆的优先级" in prompt
        assert "优先级高于长期记忆" in prompt
        # 提示里要给出冲突时的具体处理方式，而不是只说"以风格为准"
        assert "切到激进风格" in prompt
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_ai_chat_stream_bumps_hits_for_injected_memories(tmp_path, monkeypatch):
    """被注入上下文并真正用上的记忆要累计 hits，否则 hit-boost 永远是死代码。"""
    server, thread, port = _start_server(tmp_path)
    _request_json(
        "POST",
        f"http://127.0.0.1:{port}/ai/config",
        {"base_url": "http://127.0.0.1:9", "api_key": "sk-test", "model": "demo"},
    )
    ai_service = server.state.ai_service()
    monkeypatch.setattr(ai_service, "_agent", _SystemPromptStubAgent())
    _seed_memory(server)
    before = {record.id: record.hits for record in ai_service._memory.load()}
    try:
        _request_ndjson(f"http://127.0.0.1:{port}/ai/chat/stream", {"message": "再看下资金面"})
        after = {record.id: record.hits for record in ai_service._memory.load()}
        assert after and all(after[record_id] == hits + 1 for record_id, hits in before.items())
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_ai_session_history_routes(tmp_path, monkeypatch):
    server, thread, port = _start_server(tmp_path)
    base = f"http://127.0.0.1:{port}"
    _request_json(
        "POST",
        f"{base}/ai/config",
        {"base_url": "http://127.0.0.1:9", "api_key": "sk-test", "model": "demo"},
    )
    stub = _RecordingStubAgent()
    ai_service = server.state.ai_service()
    monkeypatch.setattr(ai_service, "_agent", stub)
    try:
        assert _request_json("GET", f"{base}/ai/sessions") == {"items": []}

        events = _request_ndjson(f"{base}/ai/chat/stream", {"message": "帮我看看 600519"})
        session_id = events[0]["session_id"]

        items = _request_json("GET", f"{base}/ai/sessions")["items"]
        assert [item["session_id"] for item in items] == [session_id]
        assert items[0]["message_count"] == 2
        assert items[0]["title"] == "帮我看看 600519"

        detail = _request_json("GET", f"{base}/ai/session?session_id={session_id}")
        assert detail["session_id"] == session_id
        assert [turn["role"] for turn in detail["display"]] == ["user", "assistant"]
        # 协议消息、待压缩归档与滚动纪要都不出网络边界
        assert "messages" not in detail
        assert "pending_archive" not in detail
        assert "rolling_summary" not in detail

        # 带着回读到的 session_id 续问：历史必须回到 agent 上下文
        _request_ndjson(f"{base}/ai/chat/stream", {"message": "再看下资金面", "session_id": session_id})
        assert stub.turns[1] == {"protocol": 1, "display": 2}

        status, payload = _request_json_allow_error("GET", f"{base}/ai/session?session_id=never-existed")
        assert status == 404
        assert payload["code"] == "ai_session_not_found"

        assert _request_json("POST", f"{base}/ai/session/delete", {"session_id": session_id}) == {
            "session_id": session_id,
            "deleted": True,
        }
        assert _request_json("GET", f"{base}/ai/sessions")["items"] == []

        status, payload = _request_json_allow_error("POST", f"{base}/ai/session/delete", {})
        assert status == 400
        assert payload["code"] == "validation_error"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_ai_chat_stream_emits_heartbeat_while_tools_run(tmp_path, monkeypatch):
    """长工具调用期间没有事件时，事件流必须自己保活。

    否则前端 180 秒"没收到字节即超时"会把这一轮误杀成"回答中断"，而 worker 仍在
    跑并持着会话锁，用户下一次发送要白等 90 秒。沿用同文件的 monkeypatch 范式，
    把心跳间隔压到几十毫秒，整条测试跑在 1 秒内。
    """
    from astock_backtester.ai import facade

    monkeypatch.setattr(facade, "AI_STREAM_HEARTBEAT_SECONDS", 0.05)
    server, thread, port = _start_server(tmp_path)
    base = f"http://127.0.0.1:{port}"
    _request_json(
        "POST",
        f"{base}/ai/config",
        {"base_url": "http://127.0.0.1:9", "api_key": "sk-test", "model": "demo"},
    )

    class _SlowToolAgent:
        def run(self, *, session, user_message, system_prompt, max_steps, context=None, on_event):
            on_event({"type": "tool_call", "id": "t1", "name": "run_strategy_backtest", "args": {}})
            time.sleep(0.4)  # 模拟一次跑几分钟的全市场回测
            on_event(
                {
                    "type": "tool_result",
                    "id": "t1",
                    "name": "run_strategy_backtest",
                    "ok": True,
                    "summary": "完成",
                    "duration_ms": 400,
                }
            )
            session["display"].append({"role": "assistant", "content": "回测完成", "tool_steps": [], "ts": "now"})
            return {}

    ai_service = server.state.ai_service()
    monkeypatch.setattr(ai_service, "_agent", _SlowToolAgent())
    try:
        events = _request_ndjson(f"{base}/ai/chat/stream", {"message": "跑一次全市场回测"})
        types = [event["type"] for event in events]
        assert "heartbeat" in types
        assert types[-1] == "result"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_ai_events_stream_heartbeat_and_publish(tmp_path, monkeypatch):
    from astock_backtester.ai import facade

    monkeypatch.setattr(facade, "HEARTBEAT_INTERVAL_SECONDS", 0.2)
    server, thread, port = _start_server(tmp_path)
    try:
        received: list[dict] = []

        def reader() -> None:
            request = Request(f"http://127.0.0.1:{port}/ai/events/stream", method="GET")
            with _OPENER.open(request, timeout=10) as response:
                while len(received) < 2:
                    line = response.readline()
                    if not line:
                        break
                    received.append(json.loads(line.decode("utf-8")))

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        time.sleep(0.4)
        ai_service = server.state.ai_service()
        ai_service._broker.publish({"type": "data_fresh", "module": "news"})
        reader_thread.join(timeout=5)
        types = [event["type"] for event in received]
        assert "data_fresh" in types
        assert "heartbeat" in types
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_ai_chat_request_validation_rejects_empty_message(tmp_path):
    server, thread, port = _start_server(tmp_path)
    try:
        status, payload = _request_json_allow_error("POST", f"http://127.0.0.1:{port}/ai/chat/stream", {"message": ""})
        assert status == 400
        assert payload["code"] == "validation_error"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_ai_chat_stream_without_session_id_releases_lock_after_turn(tmp_path, monkeypatch):
    """回归守卫（W1）：不带 session_id 的请求必须也置位 lock_held。

    该分支（`session_lock is None` → 新建会话后取锁）与带 session_id 的分支
    是两条独立的 `acquire` 路径。若只在一条上置位 `lock_held`，另一条就会
    “持锁但不释放” —— 该会话此后永久 `ai_session_busy`，表现为用户再也发不出
    消息，而带 session_id 的测试全都察觉不到。
    """
    server, thread, port = _start_server(tmp_path)
    try:
        _request_json(
            "POST",
            f"http://127.0.0.1:{port}/ai/config",
            {"base_url": "http://127.0.0.1:9", "api_key": "sk-test", "model": "demo"},
        )
        service = server.state.ai_service()

        class StubAgent:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def run(self, *, session, user_message, system_prompt, max_steps, context=None, on_event):
                session["display"].append({"role": "assistant", "content": "ok", "tool_steps": [], "ts": "now"})
                return {}

        monkeypatch.setattr(service, "_agent", StubAgent())

        events = _request_ndjson(f"http://127.0.0.1:{port}/ai/chat/stream", {"message": "第一次提问"})
        session_id = events[0]["session_id"]
        assert session_id

        # 该轮结束后锁必须已释放：无残留持锁、引用计数归零。
        entry = service._session_locks.get(session_id)
        assert entry is not None, "会话锁条目不应被无端丢弃"
        assert not entry.lock.locked(), f"不带 session_id 的分支没有释放锁：{entry.lock.locked()}"
        assert entry.refs == 0, f"引用计数泄漏：refs={entry.refs}"

        # 端到端验证：同一会话还能继续发消息（不会被 ai_session_busy 永久锁死）。
        again = _request_ndjson(
            f"http://127.0.0.1:{port}/ai/chat/stream",
            {"message": "第二次提问", "session_id": session_id},
        )
        assert again[-1]["type"] == "result", again
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_ai_chat_stream_bad_session_id_isolated(tmp_path):
    server, thread, port = _start_server(tmp_path)
    try:
        # 未配置 → 仍是 ai_not_configured；session_id 清洗后不落盘
        events = _request_ndjson(
            f"http://127.0.0.1:{port}/ai/chat/stream",
            {"message": "x", "session_id": "../escape"},
        )
        assert events[0]["code"] == "ai_not_configured"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_ai_config_store_keeps_embedding_base_url_when_blank(tmp_path):
    """AiConfigStore.save 必须与其他密钥字段同一套“留空保持”语义。

    HTTP 层对 embedding_base_url 做了兜底，但直接调用 store 的路径（例如
    其他调用方或未来重构）曾会把已存的独立 embedding 地址静默擦成空串。
    """
    from astock_backtester.ai.config import AiConfig, AiConfigStore

    store = AiConfigStore(tmp_path)
    store.save(
        AiConfig(
            base_url="https://api.deepseek.com/v1",
            api_key="sk-chat-123",
            model="demo",
            embedding_base_url="https://api.siliconflow.cn/v1",
            embedding_api_key="sk-embed-456",
        )
    )
    store.save(AiConfig(base_url="https://api.deepseek.com/v1", api_key="sk-chat-123", model="demo"))
    reloaded = store.load()
    assert reloaded.embedding_base_url == "https://api.siliconflow.cn/v1"
    assert reloaded.embedding_api_key == "sk-embed-456"
    assert reloaded.embedding_endpoint() == ("https://api.siliconflow.cn/v1", "sk-embed-456")

def test_ai_session_locks_are_bounded_and_reusable(tmp_path):
    """会话锁字典必须有上限，且不得误淘汰仍被持有的锁。

    长跑 sidecar 里每个新会话都会建一把锁；无上限即缓慢泄漏。这里直接验证
    ``AiService._session_lock`` 的淘汰策略：被持有的锁保留，空闲锁可回收，
    且同一 session_id 始终拿回同一把锁（互斥语义不被破坏）。
    """
    server, thread, port = _start_server(tmp_path)
    try:
        service = server.state.ai_service()
        import astock_backtester.ai.facade as facade_module

        limit = facade_module.AI_MAX_SESSION_LOCKS
        assert limit > 0

        # 制造一个“正在生成”的会话：持锁不放。
        busy_lock = service._session_lock("busy-session")
        busy_lock.acquire()
        try:
            for index in range(limit + 20):
                idle = service._session_lock(f"idle-{index}")
                # _session_lock 保留了引用；这些空闲条目要能被淘汰，先归还引用。
                idle.refs = 0
            assert len(service._session_locks) <= limit
            # 被持有的锁不能被淘汰，否则并发写同一会话的互斥会失效。
            assert service._session_locks["busy-session"] is busy_lock
            assert busy_lock.lock.locked()

            # 空闲会话仍应稳定拿到同一把锁（身份复用，不是每次新建）。
            first = service._session_lock("idle-1")
            assert service._session_lock("idle-1") is first
        finally:
            busy_lock.lock.release()
            busy_lock.refs = 0
        assert not busy_lock.lock.locked()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_ai_session_lock_not_evicted_between_fetch_and_acquire(tmp_path, monkeypatch):
    """回归守卫（B1）：锁在“已取出、还没 acquire”的窗口里不得被淘汰。

    旧实现按 ``lock.locked()`` 判空闲，fetch 与 acquire 之间的窗口里锁恰好是
    空闲的 —— 淘汰线程此时删掉条目，同一会话就会先后拿到两把互不相斥的锁，
    会话互斥静默失效。引用计数条目方案必须保证 ``refs > 0`` 永不淘汰。
    """
    server, thread, port = _start_server(tmp_path)
    try:
        service = server.state.ai_service()
        import astock_backtester.ai.facade as facade_module

        limit = facade_module.AI_MAX_SESSION_LOCKS

        # 取出 victim 的锁，但故意不 acquire —— 复刻竞态窗口。
        victim = service._session_lock("victim")
        assert victim.refs == 1

        # 再把字典塞满，逼出淘汰循环。
        for index in range(limit + 10):
            extra = service._session_lock(f"filler-{index}")
            extra.refs = 0

        # 关键断言：引用计数未归还，条目必须还在，且仍是同一把锁。
        assert service._session_locks.get("victim") is victim

        # 模拟“等了一会才 acquire”：此刻若被换过锁，就是互斥失效。
        assert victim.acquire(timeout=1.0)
        victim.release()
        assert service._session_locks.get("victim") is victim
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_ai_chat_stream_timeout_does_not_unlock_other_holder(tmp_path, monkeypatch):
    """回归守卫：acquire 超时的请求不得解开**别人**持有的会话锁。

    ``threading.Lock`` 不做持有者校验，超时分支若直接 ``release()`` 会静默解开
    正在跑的那一轮的锁 —— 下一个请求立刻拿到同一把锁，两个 worker 并发写同一
    会话（正是会话互斥要根除的“失忆”成因）。且该路径触发条件极宽松：一次超时
    即可，不需要任何高并发。
    """
    server, thread, port = _start_server(tmp_path)
    try:
        service = server.state.ai_service()
        import astock_backtester.ai.facade as facade_module

        # 必须先配置，否则 chat_stream 在拿到锁之前就抛 ai_not_configured。
        _request_json(
            "POST",
            f"http://127.0.0.1:{port}/ai/config",
            {"base_url": "http://127.0.0.1:9", "api_key": "sk-test", "model": "demo"},
        )

        # 把超时压到极短，让模拟的“上一轮仍在跑”立刻走到超时分支。
        monkeypatch.setattr(facade_module, "AI_SESSION_LOCK_TIMEOUT_SECONDS", 0.05)

        # A：模拟正在生成的那一轮（直接持锁）。
        holder = service._session_lock("race-session")
        assert holder.acquire()
        try:
            # B：同一会话发起新请求 → 应在超时后抛 AiSessionBusy。
            request = facade_module.AiChatRequest(message="hello", session_id="race-session")
            raised = None
            try:
                list(service.chat_stream(request))
            except Exception as exc:  # noqa: BLE001 - 断言类型即可
                raised = exc
            assert raised is not None, "超时后必须抛 AiSessionBusy，而不是静默继续"
            assert "AiSessionBusy" in type(raised).__name__ or "仍在生成" in str(raised)

            # 核心断言：A 的锁必须仍然被持有 —— B 没有解开它。
            assert holder.lock.locked(), "超时请求解开了他人持有的锁，互斥已失效"

            # C：此时再请求必须仍被拦住（说明互斥真的还在）。
            raised_again = None
            try:
                list(service.chat_stream(request))
            except Exception as exc:  # noqa: BLE001
                raised_again = exc
            assert raised_again is not None, "持有者未释放时，第三个请求也必须被拦住"

            # B/C 的引用计数必须已归还、不得泄漏。
            assert holder.refs == 1, f"超时路径泄漏了引用计数：refs={holder.refs}"
        finally:
            holder.lock.release()
            holder.refs = 0
        assert not holder.lock.locked()
    finally:
        server.shutdown()
        thread.join(timeout=5)
