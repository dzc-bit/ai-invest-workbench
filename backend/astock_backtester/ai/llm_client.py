"""Multi-protocol LLM client.

One :class:`ChatModel` surface, three wire protocols selected by
``AiConfig.api_style``:

- ``chat-completions`` (default): ``POST {base}/chat/completions`` via the
  official ``openai`` SDK — works with OpenAI/DeepSeek/Qwen/Moonshot/SiliconFlow
  and every other OpenAI-compatible provider.
- ``responses``: the OpenAI **Responses API** (GPT-5 era models) via the same
  SDK's ``client.responses.create``. Tool schemas and message history are
  converted to the flat Responses item format.
- ``anthropic``: the Anthropic **Messages API** (``{base}/v1/messages``) with
  hand-rolled SSE streaming (tool_use / tool_result block mapping), because the
  openai SDK does not speak Anthropic.

All three normalize to the same event protocol consumed by the agent loop:
``("text", delta)`` while streaming, then a final
``("final", {"content", "tool_calls"})`` turn. Protocol converters are pure
functions and unit tested without network.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any, Protocol

from astock_backtester.ai.config import AiConfig
from astock_backtester.ai.errors import AiNotConfigured, AiUpstreamError

ChatEvent = tuple[str, Any]
"""("text", delta) while streaming content, then ("final", turn dict)."""


class ChatModel(Protocol):
    """The Agent loop only depends on this protocol; tests swap in a fake."""

    def chat(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None) -> Iterator[ChatEvent]:
        ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


def _default_client_factory(config: AiConfig) -> Any:
    from openai import OpenAI

    return OpenAI(
        base_url=config.base_url,
        api_key=config.api_key,
        timeout=120.0,
        max_retries=2,
    )


def _map_provider_error(exc: Exception) -> AiUpstreamError:
    name = type(exc).__name__
    detail = str(exc)
    if len(detail) > 400:
        detail = detail[:400] + "..."
    return AiUpstreamError(f"模型服务调用失败（{name}）：{detail}")


# --------------------------------------------------------------------- schema
def responses_tools(schemas: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """OpenAI function schema → Responses API flat function tool."""
    flat = []
    for schema in schemas or []:
        function = schema.get("function", schema)
        flat.append(
            {
                "type": "function",
                "name": function.get("name"),
                "description": function.get("description", ""),
                "parameters": function.get("parameters", {"type": "object", "properties": {}}),
            }
        )
    return flat


def anthropic_tools(schemas: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """OpenAI function schema → Anthropic tool definition."""
    tools = []
    for schema in schemas or []:
        function = schema.get("function", schema)
        tools.append(
            {
                "name": function.get("name"),
                "description": function.get("description", ""),
                "input_schema": function.get("parameters", {"type": "object", "properties": {}}),
            }
        )
    return tools


# ------------------------------------------------------------------- messages
def responses_input(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Protocol messages → (instructions, Responses input items).

    assistant tool_calls become ``function_call`` items; tool digests become
    ``function_call_output`` items tied by call_id.
    """
    instructions = ""
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = str(message.get("content") or "")
        if role == "system":
            instructions = content if not instructions else f"{instructions}\n\n{content}"
            continue
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": str(message.get("tool_call_id", "")), "output": content})
            continue
        tool_calls = message.get("tool_calls") or []
        if role == "assistant" and tool_calls:
            if content:
                items.append({"role": "assistant", "content": content})
            for call in tool_calls:
                function = call.get("function", {})
                items.append(
                    {
                        "type": "function_call",
                        "call_id": str(call.get("id", "")),
                        "name": str(function.get("name", "")),
                        "arguments": str(function.get("arguments", "") or "{}"),
                    }
                )
            continue
        items.append({"role": "assistant" if role == "assistant" else "user", "content": content})
    return instructions, items


def _anthropic_content(message: dict[str, Any]) -> list[dict[str, Any]]:
    role = message.get("role")
    content = str(message.get("content") or "")
    if role == "tool":
        return [{"type": "tool_result", "tool_use_id": str(message.get("tool_call_id", "")), "content": content}]
    blocks: list[dict[str, Any]] = []
    if content:
        blocks.append({"type": "text", "text": content})
    if role == "assistant":
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            try:
                tool_input = json.loads(str(function.get("arguments") or "{}"))
            except json.JSONDecodeError:
                tool_input = {}
            blocks.append({"type": "tool_use", "id": str(call.get("id", "")), "name": str(function.get("name", "")), "input": tool_input})
    return blocks


def anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Protocol messages → (system, Anthropic messages) with role merging.

    Anthropic rejects consecutive same-role messages, so adjacent blocks are
    merged (this happens when an assistant text + tool_use round is followed by
    more than one tool digest).
    """
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            system_parts.append(str(message.get("content") or ""))
            continue
        blocks = _anthropic_content(message)
        if not blocks:
            continue
        wire_role = "user" if role in ("user", "tool") else "assistant"
        if converted and converted[-1]["role"] == wire_role:
            converted[-1]["content"].extend(blocks)
        else:
            converted.append({"role": wire_role, "content": blocks})
    return "\n\n".join(part for part in system_parts if part), converted


# -------------------------------------------------------------------- client
class OpenAiCompatibleClient:
    """ChatModel implementation dispatching on ``AiConfig.api_style``."""

    def __init__(
        self,
        config_provider: Callable[[], AiConfig],
        client_factory: Callable[[AiConfig], Any] | None = None,
        anthropic_post: Callable[[AiConfig, dict[str, Any]], Iterator[str]] | None = None,
    ) -> None:
        self._config_provider = config_provider
        self._client_factory = client_factory or _default_client_factory
        self._anthropic_post = anthropic_post or self._default_anthropic_post

    def _require_config(self) -> AiConfig:
        config = self._config_provider()
        if not config.is_configured():
            raise AiNotConfigured("AI 服务尚未配置，请先在设置中填写 base_url、API Key 和模型名。")
        return config

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[ChatEvent]:
        config = self._require_config()
        if config.api_style == "responses":
            yield from self._chat_responses(config, messages, tools)
        elif config.api_style == "anthropic":
            yield from self._chat_anthropic(config, messages, tools)
        else:
            yield from self._chat_completions(config, messages, tools)

    # ---------------------------------------------------- chat-completions
    def _chat_completions(
        self, config: AiConfig, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> Iterator[ChatEvent]:
        client = self._client_factory(config)
        kwargs: dict[str, Any] = {
            "model": config.model,
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
        try:
            stream = client.chat.completions.create(**kwargs)
        except Exception as exc:  # openai SDK raises SDK-specific exceptions
            raise _map_provider_error(exc) from exc

        content_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        try:
            for chunk in stream:
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                finish_reason = getattr(chunk.choices[0], "finish_reason", None) or finish_reason
                text = getattr(delta, "content", None) if delta is not None else None
                if text:
                    content_parts.append(text)
                    yield ("text", text)
                raw_calls = getattr(delta, "tool_calls", None) if delta is not None else None
                for raw in raw_calls or []:
                    slot = tool_calls.setdefault(
                        raw.index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if raw.id:
                        slot["id"] = raw.id
                    function = getattr(raw, "function", None)
                    if function is not None:
                        if function.name and not slot["function"]["name"]:
                            slot["function"]["name"] = function.name
                        if function.arguments:
                            slot["function"]["arguments"] += function.arguments
        except Exception as exc:
            raise _map_provider_error(exc) from exc

        ordered = [tool_calls[index] for index in sorted(tool_calls)]
        for index, call in enumerate(ordered):
            if not call["id"]:
                call["id"] = f"call_{index}"
        yield (
            "final",
            {
                "content": "".join(content_parts) or None,
                "tool_calls": ordered or None,
                "truncated": finish_reason == "length",
            },
        )

    # ------------------------------------------------------------ responses
    def _chat_responses(
        self, config: AiConfig, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> Iterator[ChatEvent]:
        client = self._client_factory(config)
        instructions, items = responses_input(messages)
        kwargs: dict[str, Any] = {
            "model": config.model,
            "instructions": instructions or None,
            "input": items,
            "temperature": config.temperature,
            "max_output_tokens": config.max_tokens,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = responses_tools(tools)
        try:
            stream = client.responses.create(**kwargs)
        except Exception as exc:
            raise _map_provider_error(exc) from exc

        content_parts: list[str] = []
        calls: list[dict[str, Any]] = []
        truncated = False
        try:
            for event in stream:
                event_type = getattr(event, "type", "")
                if event_type == "response.output_text.delta":
                    delta = getattr(event, "delta", None)
                    if delta:
                        content_parts.append(delta)
                        yield ("text", delta)
                elif event_type == "response.incomplete":
                    # 半截答案不能当完整结论交付
                    truncated = True
                elif event_type in {"response.failed", "error"}:
                    detail = getattr(getattr(event, "response", None), "error", None) or getattr(event, "error", None)
                    raise AiUpstreamError(f"模型服务返回失败：{detail or event_type}")
                elif event_type == "response.completed":
                    response = getattr(event, "response", None)
                    if getattr(response, "status", "") == "incomplete":
                        truncated = True
                    for item in getattr(response, "output", None) or []:
                        if getattr(item, "type", "") == "function_call":
                            calls.append(
                                {
                                    "id": getattr(item, "call_id", "") or getattr(item, "id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": getattr(item, "name", ""),
                                        "arguments": getattr(item, "arguments", "") or "{}",
                                    },
                                }
                            )
        except AiUpstreamError:
            raise
        except Exception as exc:
            raise _map_provider_error(exc) from exc
        yield (
            "final",
            {"content": "".join(content_parts) or None, "tool_calls": calls or None, "truncated": truncated},
        )

    # ------------------------------------------------------------- anthropic
    def _default_anthropic_post(self, config: AiConfig, payload: dict[str, Any]) -> Iterator[str]:
        import requests

        url = f"{config.base_url}/v1/messages"
        headers = {
            "x-api-key": config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        try:
            response = requests.post(url, json=payload, headers=headers, stream=True, timeout=(15, 180))
        except requests.RequestException as exc:
            raise _map_provider_error(exc) from exc
        if response.status_code != 200:
            body = response.text[:300]
            raise AiUpstreamError(f"模型服务调用失败（HTTP {response.status_code}）：{body}")
        try:
            for line in response.iter_lines(decode_unicode=True):
                if line:
                    yield line
        except requests.RequestException as exc:
            raise _map_provider_error(exc) from exc

    def _chat_anthropic(
        self, config: AiConfig, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> Iterator[ChatEvent]:
        system, converted = anthropic_messages(messages)
        payload: dict[str, Any] = {
            "model": config.model,
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
            "stream": True,
            "messages": converted,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = anthropic_tools(tools)

        content_parts: list[str] = []
        tool_blocks: dict[int, dict[str, Any]] = {}
        truncated = False
        try:
            for line in self._anthropic_post(config, payload):
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[len("data:") :].strip())
                except json.JSONDecodeError:
                    continue
                event_type = event.get("type")
                if event_type == "content_block_start":
                    block = event.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        tool_blocks[int(event.get("index", 0))] = {
                            "id": str(block.get("id", "")),
                            "type": "function",
                            "function": {"name": str(block.get("name", "")), "arguments": ""},
                        }
                elif event_type == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        content_parts.append(delta["text"])
                        yield ("text", delta["text"])
                    elif delta.get("type") == "input_json_delta":
                        slot = tool_blocks.get(int(event.get("index", 0)))
                        if slot is not None:
                            slot["function"]["arguments"] += str(delta.get("partial_json") or "")
                elif event_type == "message_delta":
                    if (event.get("delta") or {}).get("stop_reason") == "max_tokens":
                        truncated = True
                elif event_type == "error":
                    # 以前 error 事件落进分支空档被静默忽略，半截答案当成正常结束
                    detail = (event.get("error") or {}).get("message") or "未知上游错误"
                    raise AiUpstreamError(f"模型服务返回失败：{detail}")
        except AiUpstreamError:
            raise
        except Exception as exc:
            raise _map_provider_error(exc) from exc

        ordered = [tool_blocks[index] for index in sorted(tool_blocks)]
        yield (
            "final",
            {
                "content": "".join(content_parts) or None,
                "tool_calls": ordered or None,
                "truncated": truncated,
            },
        )

    # -------------------------------------------------------------- embed
    def embed(self, texts: list[str]) -> list[list[float]]:
        config = self._require_config()
        if not config.embedding_model.strip():
            raise AiNotConfigured("未配置 embedding_model，知识检索不可用。")
        # embedding 允许走独立供应商：未单独配置时回退主 base_url/api_key。
        embed_base_url, embed_api_key = config.embedding_endpoint()
        embed_config = AiConfig(
            base_url=embed_base_url,
            api_key=embed_api_key,
            model=config.model,
            embedding_model=config.embedding_model,
        )
        client = self._client_factory(embed_config)
        try:
            response = client.embeddings.create(model=config.embedding_model, input=texts)
        except Exception as exc:
            raise _map_provider_error(exc) from exc
        return [item.embedding for item in response.data]


def estimate_tokens(text: str) -> int:
    """Rough budget proxy: CJK ≈ 1 token/char, ASCII ≈ 4 chars/token."""
    if not text:
        return 0
    cjk = sum(1 for ch in text if ord(ch) > 0x2E80)
    return cjk + max(0, (len(text) - cjk) // 4)
