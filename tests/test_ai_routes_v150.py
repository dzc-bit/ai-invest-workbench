"""HTTP tests for the v1.5.0 AI routes: conditions/parse, insight/oneshot,
optimize grid search, plus the source-diagnostics aggregation endpoint."""

from __future__ import annotations

import json
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener

from astock_backtester.sample_data import sample_daily_bars
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
    warehouse = tmp_path / "本地数据仓"
    warehouse.mkdir(exist_ok=True)
    server = create_server(host="127.0.0.1", port=0, cache_dir=warehouse)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    return server, thread, server.server_address[1]


def _configure_ai(server) -> None:
    _request_json(
        "POST",
        f"http://127.0.0.1:{server.server_address[1]}/ai/config",
        {"base_url": "http://127.0.0.1:9", "api_key": "sk-test", "model": "demo"},
    )


def _configure_style(server, style: str) -> None:
    """/ai/config 是整体覆盖语义：只发 research_style 会把 base_url 清空。"""
    _request_json(
        "POST",
        f"http://127.0.0.1:{server.server_address[1]}/ai/config",
        {"base_url": "http://127.0.0.1:9", "api_key": "sk-test", "model": "demo", "research_style": style},
    )


class FakeModel:
    """Scripted chat model returning one canned final content per call."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def chat(self, messages, *, tools=None):
        self.calls.append(messages)
        content = self.replies.pop(0) if self.replies else ""
        yield ("final", {"content": content, "tool_calls": None})

    def embed(self, texts):
        return [[0.0] * 4 for _ in texts]


def _stub_model(server, monkeypatch, model: FakeModel) -> None:
    ai_service = server.state.ai_service()
    monkeypatch.setattr(ai_service, "_model", model)


def test_conditions_parse_requires_configuration(tmp_path):
    server, thread, port = _start_server(tmp_path)
    try:
        status, payload = _request_json_allow_error(
            "POST", f"http://127.0.0.1:{port}/ai/conditions/parse", {"text": "近5天放量上涨"}
        )
        assert status == 400
        assert payload["code"] == "ai_not_configured"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_conditions_parse_rejects_empty_text(tmp_path):
    server, thread, port = _start_server(tmp_path)
    _configure_ai(server)
    try:
        status, payload = _request_json_allow_error(
            "POST", f"http://127.0.0.1:{port}/ai/conditions/parse", {"text": "   "}
        )
        assert status == 400
        assert payload["code"] == "validation_error"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_conditions_parse_validates_and_returns_lists(tmp_path, monkeypatch):
    server, thread, port = _start_server(tmp_path)
    _configure_ai(server)
    reply = json.dumps(
        {
            "entry_expressions": ["量比2日介于1.2到2.5", "近5日主力净流入大于300万"],
            "exit_expressions": ["MACD死叉"],
            "approximations": ["『放量』→量比2日介于1.2到2.5"],
        },
        ensure_ascii=False,
    )
    _stub_model(server, monkeypatch, FakeModel([f"```json\n{reply}\n```"]))
    try:
        result = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/ai/conditions/parse",
            {"text": "近5天放量上涨、主力净流入为正，破位就卖"},
        )
        assert [node["condition_id"] for node in result["entry"]] == [
            "volume_ratio_between",
            "capital_flow_n_day_sum_at_least",
        ]
        assert result["entry"][0]["params"] == {"window": 2, "min": 1.2, "max": 2.5}
        assert result["entry"][0]["expression"] == "量比2日介于1.2到2.5"
        assert [node["condition_id"] for node in result["exit"]] == ["macd_dead_cross"]
        assert result["approximations"] == ["『放量』→量比2日介于1.2到2.5"]
        assert result["dropped"] == []
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_conditions_parse_self_heals_invalid_conditions_with_retry(tmp_path, monkeypatch):
    server, thread, port = _start_server(tmp_path)
    _configure_ai(server)
    first = json.dumps(
        {
            "entry_expressions": ["近5天放量上涨"],  # not a valid DSL line
            "exit_expressions": [],
            "approximations": [],
        },
        ensure_ascii=False,
    )
    second = json.dumps(
        {
            "entry_expressions": ["量比5日介于1.2到2.5"],
            "exit_expressions": ["收盘价跌破20日均线"],
            "approximations": ["『放量』→量比5日介于1.2到2.5"],
        },
        ensure_ascii=False,
    )
    model = FakeModel([first, second])
    _stub_model(server, monkeypatch, model)
    try:
        result = _request_json(
            "POST", f"http://127.0.0.1:{port}/ai/conditions/parse", {"text": "近5天放量上涨，破20日线卖"}
        )
        assert [node["expression"] for node in result["entry"]] == ["量比5日介于1.2到2.5"]
        assert [node["expression"] for node in result["exit"]] == ["收盘价跌破20日均线"]
        assert result["dropped"] == []
        assert len(model.calls) == 2  # initial proposal + one self-healing retry
        retry_message = model.calls[1][-1]["content"]
        assert "没有通过本地校验" in retry_message
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_conditions_parse_reports_persistently_invalid_lines_as_dropped(tmp_path, monkeypatch):
    server, thread, port = _start_server(tmp_path)
    _configure_ai(server)
    broken = json.dumps(
        {
            "entry_expressions": ["KDJ金叉"],  # unsupported indicator, never valid
            "exit_expressions": [],
            "approximations": [],
        },
        ensure_ascii=False,
    )
    model = FakeModel([broken, broken, broken])
    _stub_model(server, monkeypatch, model)
    try:
        result = _request_json("POST", f"http://127.0.0.1:{port}/ai/conditions/parse", {"text": "KDJ金叉买入"})
        assert result["entry"] == []
        assert len(result["dropped"]) == 1
        assert result["dropped"][0]["kind"] == "entry"
        assert len(model.calls) == 3  # 1 + MAX_PARSE_ATTEMPTS(2) retries
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_insight_oneshot_requires_configuration_and_known_scene(tmp_path):
    server, thread, port = _start_server(tmp_path)
    try:
        status, payload = _request_json_allow_error(
            "POST", f"http://127.0.0.1:{port}/ai/insight/oneshot", {"scene": "results_overview", "context": {}}
        )
        assert status == 400
        assert payload["code"] == "ai_not_configured"

        _configure_ai(server)
        status, payload = _request_json_allow_error(
            "POST", f"http://127.0.0.1:{port}/ai/insight/oneshot", {"scene": "nope", "context": {}}
        )
        assert status == 400
        assert payload["code"] == "validation_error"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_insight_oneshot_returns_single_paragraph(tmp_path, monkeypatch):
    server, thread, port = _start_server(tmp_path)
    _configure_ai(server)
    _stub_model(server, monkeypatch, FakeModel(["本次回测收益 3.2%、回撤 -1.8%，交易仅 1 笔，样本过少，建议扩大日期范围再评估。"]))
    try:
        result = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/ai/insight/oneshot",
            {"scene": "results_overview", "context": {"total_return_pct": 0.032}},
        )
        assert result["ok"] is True
        assert result["scene"] == "results_overview"
        assert "3.2%" in result["text"]
        assert result["generated_at"]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_insight_oneshot_carries_the_configured_research_style(tmp_path, monkeypatch):
    """风格只作用于 chat 时，切风格对点评/寻优/复盘毫无影响——这是"三种风格没差别"
    的一半来源。点评必须把当前风格指令一起送进提示词。"""
    server, thread, port = _start_server(tmp_path)
    _configure_ai(server)
    model = FakeModel(["情绪处于发酵期，晋级率尚可，接力需控制仓位。"])
    _stub_model(server, monkeypatch, model)
    try:
        _configure_style(server, "aggressive")
        _request_json(
            "POST",
            f"http://127.0.0.1:{port}/ai/insight/oneshot",
            {"scene": "results_overview", "context": {"total_return_pct": 0.032}},
        )
        prompt = model.calls[-1][0]["content"]
        assert "龙头选手" in prompt
        assert "价值" in prompt  # 明确不做价值型评论
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_data_coverage_oneshot_ignores_trading_style(tmp_path, monkeypatch):
    """覆盖诊断与交易风格无关：注入风格指令反而是噪声。"""
    server, thread, port = _start_server(tmp_path)
    _configure_ai(server)
    model = FakeModel(["资金流字段缺失，点“补齐资金流”即可。"])
    _stub_model(server, monkeypatch, model)
    try:
        _configure_style(server, "aggressive")
        _request_json(
            "POST",
            f"http://127.0.0.1:{port}/ai/insight/oneshot",
            {"scene": "data_coverage", "context": {"symbols": 1}},
        )
        prompt = model.calls[-1][0]["content"]
        assert "视角：" not in prompt
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _optimize_payload():
    strategy = {
        "name": "寻优策略",
        "market_filters": [],
        "entry_groups": [
            {
                "id": "entry",
                "operator": "and",
                "conditions": [
                    {
                        "id": "c1",
                        "condition_id": "close_above_ma",
                        "enabled": True,
                        "params": {"window": 3},
                        "data_lag_days": 0,
                        "expression": "收盘价站上3日均线",
                    }
                ],
            }
        ],
        "exit_rules": [],
        "score_threshold": None,
    }
    settings = {
        "start_date": "2024-01-02",
        "end_date": "2024-01-08",
        "initial_cash": 1_000_000,
        "stock_pool": "all",
        "custom_symbols": [],
        "max_positions": 5,
        "max_daily_buys": 3,
        "fixed_holding_days": 2,
        "min_listing_days": 0,
    }
    return {
        "strategy": strategy,
        "settings": settings,
        "grid": {"fixed_holding_days": [1, 2, 3], "max_positions": [2, 3]},
    }


def test_optimize_validates_grid_before_streaming(tmp_path):
    server, thread, port = _start_server(tmp_path)
    try:
        payload = _optimize_payload()
        payload["grid"] = {"fixed_holding_days": [1] * 7, "max_positions": [2] * 7}
        status, body = _request_json_allow_error("POST", f"http://127.0.0.1:{port}/ai/optimize", payload)
        assert status == 400
        assert body["code"] == "grid_too_large"

        payload = _optimize_payload()
        payload["grid"] = {"entry_window": [3, 5]}
        status, body = _request_json_allow_error("POST", f"http://127.0.0.1:{port}/ai/optimize", payload)
        assert status == 400
        assert body["code"] == "validation_error"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_optimize_streams_combinations_and_result(tmp_path):
    server, thread, port = _start_server(tmp_path)
    server.state.warehouse.write_daily_bars(sample_daily_bars())
    try:
        events = _request_ndjson(f"http://127.0.0.1:{port}/ai/optimize", _optimize_payload())
        types = [event["type"] for event in events]
        assert types[0] == "phase"
        assert "combination" in types and "progress" in types
        assert types[-1] == "result"
        result = events[-1]["result"]
        assert result["total"] == 6
        assert result["evaluated"] == 6
        assert len(result["combinations"]) == 6
        assert result["insight"] is None  # AI not configured: sweep still completes
        assert result["insight_error"]
        combo = result["combinations"][0]
        assert combo["params"] == {"fixed_holding_days": 1, "max_positions": 2}
        assert "total_return_pct" in combo["metrics"]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_optimize_appends_ai_insight_when_configured(tmp_path, monkeypatch):
    server, thread, port = _start_server(tmp_path)
    server.state.warehouse.write_daily_bars(sample_daily_bars())
    _configure_ai(server)
    _stub_model(server, monkeypatch, FakeModel(["持仓 3 天的组合在本次区间表现最好，但组合数过少，注意过拟合。"]))
    try:
        events = _request_ndjson(f"http://127.0.0.1:{port}/ai/optimize", _optimize_payload())
        result = events[-1]["result"]
        assert "过拟合" in result["insight"]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_diagnostics_sources_aggregates_provider_state(tmp_path):
    server, thread, port = _start_server(tmp_path)
    try:
        payload = _request_json("GET", f"http://127.0.0.1:{port}/diagnostics/sources")
        assert payload["ok"] is True
        names = {source["source"] for source in payload["sources"]}
        assert names == {"realtime", "news", "finance"}
        for source in payload["sources"]:
            assert source["ok"] is False
            assert source["seconds_since_success"] is None
    finally:
        server.shutdown()
        thread.join(timeout=5)
