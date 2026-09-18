from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from astock_backtester.ai.config import AiConfig
from astock_backtester.ai.errors import AiUpstreamError
from astock_backtester.ai.llm_client import (
    OpenAiCompatibleClient,
    anthropic_messages,
    anthropic_tools,
    responses_input,
    responses_tools,
)

OPENAI_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "查行情",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}},
        },
    }
]


def test_responses_tools_flattens_schema():
    flat = responses_tools(OPENAI_SCHEMA)
    assert flat[0]["name"] == "lookup"
    assert flat[0]["parameters"]["properties"] == {"symbol": {"type": "string"}}
    assert "function" not in flat[0]


def test_anthropic_tools_maps_input_schema():
    tools = anthropic_tools(OPENAI_SCHEMA)
    assert tools[0]["name"] == "lookup"
    assert tools[0]["input_schema"]["type"] == "object"
    assert "function" not in tools[0]


def test_responses_input_splits_system_and_tool_items():
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "看下 600519"},
        {
            "role": "assistant",
            "content": "先查行情",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": '{"symbol": "600519"}'}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "price=1500"},
    ]
    instructions, items = responses_input(messages)
    assert instructions == "SYS"
    assert items[0] == {"role": "user", "content": "看下 600519"}
    assert items[1] == {"role": "assistant", "content": "先查行情"}
    assert items[2] == {"type": "function_call", "call_id": "c1", "name": "lookup", "arguments": '{"symbol": "600519"}'}
    assert items[3] == {"type": "function_call_output", "call_id": "c1", "output": "price=1500"}


def test_anthropic_messages_merges_adjacent_tool_results():
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "看下 600519"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
        {"role": "tool", "tool_call_id": "c2", "content": "r2"},
    ]
    system, converted = anthropic_messages(messages)
    assert system == "SYS"
    assert len(converted) == 3  # user / assistant(tool_use x2) / user(tool_result x2 合并)
    assert converted[1]["content"][0]["type"] == "tool_use"
    assert converted[2]["content"][0]["type"] == "tool_result"
    assert converted[2]["content"][1]["tool_use_id"] == "c2"


def _responses_event(event_type: str, **kwargs):
    return SimpleNamespace(type=event_type, **kwargs)


def test_chat_responses_streams_text_and_collects_tool_calls():
    events = [
        _responses_event("response.output_text.delta", delta="你好"),
        _responses_event("response.output_text.delta", delta="，正在查"),
        _responses_event(
            "response.completed",
            response=SimpleNamespace(
                output=[
                    SimpleNamespace(type="message", content=[]),
                    SimpleNamespace(type="function_call", call_id="rc1", id="item_1", name="lookup", arguments='{"symbol": "600519"}'),
                ]
            ),
        ),
    ]

    def factory(config):
        return SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: iter(events)))

    client = OpenAiCompatibleClient(
        lambda: AiConfig(base_url="http://x", api_key="k", model="m", api_style="responses"), client_factory=factory
    )
    events_out = list(client.chat([{"role": "user", "content": "hi"}], tools=OPENAI_SCHEMA))
    assert events_out[0] == ("text", "你好")
    final = events_out[-1][1]
    assert final["content"] == "你好，正在查"
    assert final["tool_calls"][0]["id"] == "rc1"
    assert final["tool_calls"][0]["function"]["name"] == "lookup"


def test_chat_anthropic_parses_sse_stream():
    def sse(payload: dict) -> str:
        return "data: " + json.dumps(payload, ensure_ascii=False)

    sse_lines = [
        sse({"type": "message_start"}),
        sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "查一下"}}),
        sse({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tu_1", "name": "lookup"}}),
        sse({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"symbol":'}}),
        sse({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ' "600519"}'}}),
        sse({"type": "message_stop"}),
    ]

    captured: dict = {}

    def fake_post(config: AiConfig, payload: dict):
        captured["payload"] = payload
        yield from sse_lines

    client = OpenAiCompatibleClient(
        lambda: AiConfig(base_url="https://api.anthropic.com", api_key="k", model="claude-x", api_style="anthropic"),
        anthropic_post=fake_post,
    )
    events_out = list(client.chat([{"role": "user", "content": "hi"}], tools=OPENAI_SCHEMA))
    assert events_out[0] == ("text", "查一下")
    final = events_out[-1][1]
    assert final["content"] == "查一下"
    assert final["tool_calls"][0]["id"] == "tu_1"
    assert json.loads(final["tool_calls"][0]["function"]["arguments"]) == {"symbol": "600519"}
    payload = captured["payload"]
    assert not payload.get("system")
    assert payload["tools"][0]["input_schema"]["type"] == "object"
    assert payload["messages"][0]["role"] == "user"
    assert final["truncated"] is False
    assert payload["max_tokens"] == 4096


def test_truncated_flag_maps_per_protocol():
    """被输出上限截断的回答必须带 truncated，不能被当完整结论交付。"""

    def sse(payload: dict) -> str:
        return "data: " + json.dumps(payload, ensure_ascii=False)

    anthropic_client = OpenAiCompatibleClient(
        lambda: AiConfig(base_url="https://api.anthropic.com", api_key="k", model="claude-x", api_style="anthropic"),
        anthropic_post=lambda config, payload: iter(
            [
                sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "只写到一半"}}),
                sse({"type": "message_delta", "delta": {"stop_reason": "max_tokens"}}),
            ]
        ),
    )
    anthropic_final = list(anthropic_client.chat([{"role": "user", "content": "hi"}]))[-1][1]
    assert anthropic_final["truncated"] is True

    events = [
        SimpleNamespace(type="response.output_text.delta", delta="半截"),
        SimpleNamespace(type="response.incomplete"),
    ]
    responses_client = OpenAiCompatibleClient(
        lambda: AiConfig(base_url="http://x", api_key="k", model="m", api_style="responses"),
        client_factory=lambda config: SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: iter(events))),
    )
    responses_final = list(responses_client.chat([{"role": "user", "content": "hi"}]))[-1][1]
    assert responses_final["truncated"] is True
    assert responses_final["content"] == "半截"


def test_chat_anthropic_error_event_raises_instead_of_passing_silently():
    """SSE 里的 error 事件（如 overloaded_error）以前落进分支空档被静默忽略。"""

    def fake_post(config: AiConfig, payload: dict):
        yield 'data: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "写到这"}}'
        yield 'data: {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}'

    client = OpenAiCompatibleClient(
        lambda: AiConfig(base_url="https://api.anthropic.com", api_key="k", model="claude-x", api_style="anthropic"),
        anthropic_post=fake_post,
    )
    with pytest.raises(AiUpstreamError, match="Overloaded"):
        list(client.chat([{"role": "user", "content": "hi"}]))


def test_chat_anthropic_maps_http_error_to_upstream():
    import requests as requests_lib

    class FailingPost:
        def __enter__(self):
            raise RuntimeError("should not be used")

    def fake_post(config: AiConfig, payload: dict):
        raise requests_lib.ConnectionError("boom")
        yield  # pragma: no cover

    client = OpenAiCompatibleClient(
        lambda: AiConfig(base_url="https://api.anthropic.com", api_key="k", model="m", api_style="anthropic"),
        anthropic_post=fake_post,
    )
    with pytest.raises(AiUpstreamError):
        list(client.chat([{"role": "user", "content": "hi"}]))


def test_unknown_api_style_falls_back_to_chat_completions(tmp_path):
    config = AiConfig(base_url="http://x", api_key="k", model="m", api_style="bogus").sanitized()
    assert config.api_style == "chat-completions"
