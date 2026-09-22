from __future__ import annotations

from types import SimpleNamespace

import pytest
from astock_backtester.ai.config import AiConfig
from astock_backtester.ai.errors import AiNotConfigured, AiUpstreamError
from astock_backtester.ai.llm_client import OpenAiCompatibleClient, estimate_tokens


def _configured_config(**overrides) -> AiConfig:
    values = {"base_url": "http://127.0.0.1:9", "api_key": "sk-test", "model": "demo"}
    values.update(overrides)
    return AiConfig(**values)


def _chunk(content=None, tool_calls=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _tool_delta(index, *, id=None, name=None, arguments=None):
    return SimpleNamespace(index=index, id=id, function=SimpleNamespace(name=name, arguments=arguments))


def test_chat_streams_text_and_accumulates_tool_calls():
    responses = [
        _chunk(content="你好"),
        _chunk(tool_calls=[_tool_delta(0, id="c1", name="lookup", arguments='{"a"')]),
        _chunk(tool_calls=[_tool_delta(0, arguments=":1}")]),
        _chunk(tool_calls=[_tool_delta(1, id="c2", name="second", arguments="{}")]),
    ]
    captured = {}

    def factory(config):
        captured["config"] = config
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: iter(responses))))

    client = OpenAiCompatibleClient(lambda: _configured_config(), client_factory=factory)
    events = list(client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}]))

    assert events[0] == ("text", "你好")
    final_type, final = events[-1]
    assert final_type == "final"
    assert final["content"] == "你好"
    assert [call["id"] for call in final["tool_calls"]] == ["c1", "c2"]
    assert final["tool_calls"][0]["function"]["arguments"] == '{"a":1}'
    assert captured["config"].model == "demo"


def test_chat_maps_provider_errors_to_upstream_error():
    def factory(config):
        def create(**kwargs):
            raise RuntimeError("connection refused")

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    client = OpenAiCompatibleClient(lambda: _configured_config(), client_factory=factory)
    with pytest.raises(AiUpstreamError):
        list(client.chat([{"role": "user", "content": "hi"}]))


def test_chat_requires_configuration():
    client = OpenAiCompatibleClient(lambda: AiConfig())
    with pytest.raises(AiNotConfigured):
        list(client.chat([{"role": "user", "content": "hi"}]))


def test_embed_requires_embedding_model():
    client = OpenAiCompatibleClient(lambda: _configured_config())
    with pytest.raises(AiNotConfigured):
        client.embed(["text"])


def test_embed_returns_vectors():
    fake_client = SimpleNamespace(
        embeddings=SimpleNamespace(create=lambda model, input: SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2])]))
    )
    client = OpenAiCompatibleClient(
        lambda: _configured_config(embedding_model="embed-demo"), client_factory=lambda config: fake_client
    )
    assert client.embed(["text"]) == [[0.1, 0.2]]


def test_estimate_tokens_counts_cjk_and_ascii():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("abcd") == 1
