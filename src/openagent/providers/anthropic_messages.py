"""Anthropic Messages adapter (spec §13, §18–§19).

Anthropic returns tool calls as ``tool_use`` content blocks; OpenAgent runs the tool and sends the
result back as a ``tool_result`` block in a new user message. Also used (via base-URL swap) by
Anthropic-compatible providers like DeepSeek, GLM, and MiniMax. For MiniMax fidelity (spec §19), the
assistant's full content-block list is preserved on :attr:`Message.raw_blocks` and echoed back.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ..core.errors import ErrorType
from ..core.events import ModelEventType, NormalizedModelEvent, TokenUsage, ToolCall
from ..core.models import ModelCapabilities, RemoteModel
from .base import (
    HealthResult,
    Message,
    NormalizedModelRequest,
    Role,
    TokenEstimate,
    collect,
    rough_token_estimate,
)
from .transport import Transport, TransportError

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicMessagesAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        provider_type: str = "anthropic",
        extra_headers: dict[str, str] | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.provider_type = provider_type
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if api_key:
            headers["x-api-key"] = api_key
        if extra_headers:
            headers.update(extra_headers)
        self.transport = transport or Transport(base_url=base_url.rstrip("/"), headers=headers)

    # ------------------------------------------------------------------ health/discovery

    async def test_connection(self) -> HealthResult:
        request = NormalizedModelRequest(
            model="probe", messages=[Message(role=Role.USER, content="ping")], max_tokens=1,
            stream=False,
        )
        try:
            result = await collect(self.stream_response(request))
        except TransportError as exc:
            return HealthResult(ok=False, detail=exc.message)
        if result.is_error and result.error_type == ErrorType.AUTHENTICATION_FAILED.value:
            return HealthResult(ok=False, detail="authentication failed")
        # A model-not-found / invalid-request means creds are fine but "probe" isn't a real model.
        return HealthResult(ok=True, detail="reachable")

    async def list_models(self) -> list[RemoteModel]:
        try:
            data = await self.transport.get_json("/v1/models")
        except TransportError:
            return []
        items = data.get("data", [])
        return [RemoteModel(id=i["id"], display_name=i.get("display_name", i["id"]))
                for i in items if i.get("id")]

    async def probe_model(self, model_id: str) -> ModelCapabilities:
        caps = ModelCapabilities(text=True)
        request = NormalizedModelRequest(
            model=model_id, system="Reply with OK.",
            messages=[Message(role=Role.USER, content="Say OK.")], max_tokens=16, stream=False,
        )
        try:
            result = await collect(self.stream_response(request))
            caps.text = not result.is_error and bool(result.text)
            caps.system_prompt = True
            caps.streaming = True
        except TransportError:
            caps.text = False
        return caps

    async def count_tokens(self, request: NormalizedModelRequest) -> TokenEstimate:
        try:
            payload = self._build_payload(request)
            payload.pop("max_tokens", None)
            payload.pop("stream", None)
            data = await self.transport.post_json("/v1/messages/count_tokens", payload)
            return TokenEstimate(input_tokens=data.get("input_tokens", 0))
        except TransportError:
            return rough_token_estimate(request)

    # ------------------------------------------------------------------ streaming

    async def stream_response(
        self, request: NormalizedModelRequest
    ) -> AsyncIterator[NormalizedModelEvent]:
        payload = self._build_payload(request)
        try:
            if request.stream:
                async for event in self._stream(payload):
                    yield event
            else:
                async for event in self._complete(payload):
                    yield event
        except TransportError as exc:
            yield NormalizedModelEvent(
                type=ModelEventType.ERROR, error_type=exc.error_type.value, error_message=exc.message
            )

    async def _complete(self, payload: dict[str, Any]) -> AsyncIterator[NormalizedModelEvent]:
        payload.pop("stream", None)
        data = await self.transport.post_json("/v1/messages", payload)
        response_id = data.get("id")
        for block in data.get("content") or []:
            if block.get("type") == "text" and block.get("text"):
                yield NormalizedModelEvent(
                    type=ModelEventType.TEXT_DELTA, text=block["text"], response_id=response_id
                )
            elif block.get("type") == "tool_use":
                yield NormalizedModelEvent(
                    type=ModelEventType.TOOL_CALL,
                    tool_call=ToolCall(
                        id=block.get("id", "toolu_0"),
                        name=block.get("name", ""),
                        arguments=block.get("input") or {},
                    ),
                    response_id=response_id,
                )
        if data.get("usage"):
            yield NormalizedModelEvent(type=ModelEventType.USAGE, usage=_parse_usage(data["usage"]))
        yield NormalizedModelEvent(type=ModelEventType.DONE, response_id=response_id)

    async def _stream(self, payload: dict[str, Any]) -> AsyncIterator[NormalizedModelEvent]:
        payload["stream"] = True
        blocks: dict[int, dict[str, Any]] = {}
        usage = TokenUsage()
        response_id: str | None = None
        async for evt in self.transport.stream_sse("/v1/messages", payload):
            etype = evt.get("type")
            if etype == "message_start":
                message = evt.get("message", {})
                response_id = message.get("id", response_id)
                usage.input_tokens = (message.get("usage") or {}).get("input_tokens", 0)
            elif etype == "content_block_start":
                blocks[evt["index"]] = dict(evt.get("content_block", {}))
                blocks[evt["index"]].setdefault("input_json", "")
            elif etype == "content_block_delta":
                delta = evt.get("delta", {})
                idx = evt["index"]
                if delta.get("type") == "text_delta" and delta.get("text"):
                    yield NormalizedModelEvent(
                        type=ModelEventType.TEXT_DELTA, text=delta["text"], response_id=response_id
                    )
                elif delta.get("type") == "input_json_delta":
                    blocks.setdefault(idx, {"input_json": ""})
                    blocks[idx]["input_json"] += delta.get("partial_json", "")
            elif etype == "message_delta":
                usage.output_tokens = (evt.get("usage") or {}).get("output_tokens", usage.output_tokens)
        for idx in sorted(blocks):
            block = blocks[idx]
            if block.get("type") == "tool_use":
                yield NormalizedModelEvent(
                    type=ModelEventType.TOOL_CALL,
                    tool_call=ToolCall(
                        id=block.get("id", f"toolu_{idx}"),
                        name=block.get("name", ""),
                        arguments=_loads(block.get("input_json", "")),
                    ),
                    response_id=response_id,
                )
        yield NormalizedModelEvent(type=ModelEventType.USAGE, usage=usage)
        yield NormalizedModelEvent(type=ModelEventType.DONE, response_id=response_id)

    # ------------------------------------------------------------------ payload

    def _build_payload(self, request: NormalizedModelRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": _to_anthropic_messages(request.messages),
        }
        if request.system:
            payload["system"] = request.system
        if request.temperature is not None:
            payload["temperature"] = max(0.0, min(1.0, request.temperature))
        if request.tools:
            payload["tools"] = [
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("parameters", {"type": "object", "properties": {}}),
                }
                for tool in request.tools
            ]
        return payload


def _to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.role.value if hasattr(msg.role, "value") else msg.role
        if role == Role.TOOL.value:
            out.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": msg.tool_call_id, "content": msg.content}
                    ],
                }
            )
        elif role == Role.ASSISTANT.value:
            if msg.raw_blocks is not None:  # preserve exact blocks (spec §19 MiniMax)
                out.append({"role": "assistant", "content": msg.raw_blocks})
                continue
            content: list[dict[str, Any]] = []
            if msg.content:
                content.append({"type": "text", "text": msg.content})
            for call in msg.tool_calls:
                content.append(
                    {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
                )
            out.append({"role": "assistant", "content": content or [{"type": "text", "text": ""}]})
        else:
            out.append({"role": "user", "content": msg.content})
    return out


def _parse_usage(usage: dict[str, Any]) -> TokenUsage:
    return TokenUsage(
        input_tokens=usage.get("input_tokens", 0),
        cached_input_tokens=usage.get("cache_read_input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
    )


def _loads(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {}
