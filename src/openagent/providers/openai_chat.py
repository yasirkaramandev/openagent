"""OpenAI Chat Completions adapter (spec §12, §15–§24).

This is the workhorse: DeepSeek, Qwen, Kimi, GLM, MiniMax, OpenRouter, Ollama, Mistral, Together,
Fireworks and more all speak this protocol with small deviations captured by a
:class:`CompatibilityProfile`. First-party OpenAI can also use it (Responses is preferred — see
``openai_responses.py``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ..core.errors import ErrorType
from ..core.events import (
    ModelEventType,
    NormalizedModelEvent,
    TokenUsage,
    ToolCall,
)
from ..core.models import ModelCapabilities, RemoteModel
from .base import (
    HealthResult,
    NormalizedModelRequest,
    Role,
    TokenEstimate,
    default_probe,
    rough_token_estimate,
)
from .compat.profiles import CompatibilityProfile, get_compat
from .transport import Transport, TransportError


class OpenAIChatAdapter:
    """Adapter for any OpenAI Chat Completions-compatible endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        provider_type: str = "openai",
        extra_headers: dict[str, str] | None = None,
        compat: CompatibilityProfile | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.provider_type = provider_type
        self.compat = compat or get_compat(provider_type)
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if extra_headers:
            headers.update(extra_headers)
        self.transport = transport or Transport(base_url=base_url.rstrip("/"), headers=headers)

    # ------------------------------------------------------------------ health/discovery

    async def test_connection(self) -> HealthResult:
        try:
            await self.transport.get_json("/models")
            return HealthResult(ok=True, detail="reachable")
        except TransportError as exc:
            if exc.error_type is ErrorType.MODEL_NOT_FOUND:
                # some servers lack /models but are otherwise healthy
                return HealthResult(ok=True, detail="no /models endpoint")
            return HealthResult(ok=False, detail=exc.message)

    async def list_models(self) -> list[RemoteModel]:
        data = await self.transport.get_json("/models")
        items = data.get("data", data if isinstance(data, list) else [])
        models: list[RemoteModel] = []
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                # Preserve ``owned_by`` (the publisher the catalog reports) so a mixed catalog like
                # NVIDIA Build can be filtered by publisher, and never assumed chat-compatible (§14.1).
                models.append(
                    RemoteModel(
                        id=item["id"],
                        display_name=item.get("id"),
                        owned_by=item.get("owned_by"),
                        context_window=item.get("context_window") or item.get("context_length"),
                    )
                )
        return models

    async def probe_model(self, model_id: str) -> ModelCapabilities:
        """Capability probe (spec §25.2). Only claims capabilities actually observed (item 9)."""

        return await default_probe(self, model_id)

    async def count_tokens(self, request: NormalizedModelRequest) -> TokenEstimate:
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
                type=ModelEventType.ERROR,
                error_type=exc.error_type.value,
                error_message=exc.message,
            )

    async def _complete(self, payload: dict[str, Any]) -> AsyncIterator[NormalizedModelEvent]:
        payload.pop("stream", None)
        payload.pop("stream_options", None)
        data = await self.transport.post_json("/chat/completions", payload)
        response_id = data.get("id")
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {})
        text = message.get("content") or ""
        if text:
            yield NormalizedModelEvent(
                type=ModelEventType.TEXT_DELTA, text=text, response_id=response_id
            )
        for call in message.get("tool_calls") or []:
            yield NormalizedModelEvent(
                type=ModelEventType.TOOL_CALL,
                tool_call=_parse_tool_call(call),
                response_id=response_id,
            )
        if data.get("usage"):
            yield NormalizedModelEvent(type=ModelEventType.USAGE, usage=_parse_usage(data["usage"]))
        yield NormalizedModelEvent(type=ModelEventType.DONE, response_id=response_id)

    async def _stream(self, payload: dict[str, Any]) -> AsyncIterator[NormalizedModelEvent]:
        payload["stream"] = True
        if self.compat.stream_usage:
            payload["stream_options"] = {"include_usage": True}
        tool_frags: dict[int, dict[str, Any]] = {}
        usage: TokenUsage | None = None
        response_id: str | None = None
        async for chunk in self.transport.stream_sse("/chat/completions", payload):
            response_id = chunk.get("id", response_id)
            if chunk.get("usage"):
                usage = _parse_usage(chunk["usage"])
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                # NB: only ``content`` is surfaced. NVIDIA (and some others) emit a separate
                # ``reasoning_content`` delta — that is raw chain-of-thought and is deliberately
                # ignored here so it never reaches an event, artifact, or the UI (spec §12).
                if delta.get("content"):
                    yield NormalizedModelEvent(
                        type=ModelEventType.TEXT_DELTA,
                        text=delta["content"],
                        response_id=response_id,
                    )
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    frag = tool_frags.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        frag["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        frag["name"] = fn["name"]
                    if fn.get("arguments"):
                        frag["arguments"] += fn["arguments"]
        for idx in sorted(tool_frags):
            frag = tool_frags[idx]
            yield NormalizedModelEvent(
                type=ModelEventType.TOOL_CALL,
                tool_call=ToolCall(
                    id=frag["id"] or f"call_{idx}",
                    name=frag["name"],
                    arguments=_loads_args(frag["arguments"]),
                ),
                response_id=response_id,
            )
        if usage is not None:
            yield NormalizedModelEvent(type=ModelEventType.USAGE, usage=usage)
        yield NormalizedModelEvent(type=ModelEventType.DONE, response_id=response_id)

    # ------------------------------------------------------------------ payload

    def _build_payload(self, request: NormalizedModelRequest) -> dict[str, Any]:
        messages = _to_openai_messages(request)
        payload: dict[str, Any] = {"model": request.model, "messages": messages}
        payload[self.compat.max_tokens_field] = request.max_tokens
        temp = self.compat.clamp_temperature(request.temperature)
        if temp is not None:
            payload["temperature"] = temp
        if request.tools:
            payload["tools"] = [{"type": "function", "function": tool} for tool in request.tools]
            choice = self.compat.normalize_tool_choice("auto")
            if choice is not None:
                payload["tool_choice"] = choice
        for param in self.compat.drop_params:
            payload.pop(param, None)
        return payload


def _to_openai_messages(request: NormalizedModelRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    for msg in request.messages:
        role = msg.role.value if hasattr(msg.role, "value") else msg.role
        if role == Role.TOOL.value:
            messages.append(
                {"role": "tool", "tool_call_id": msg.tool_call_id, "content": msg.content}
            )
        elif role == Role.ASSISTANT.value and msg.tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                        for call in msg.tool_calls
                    ],
                }
            )
        else:
            messages.append({"role": role, "content": msg.content})
    return messages


def _parse_tool_call(call: dict[str, Any]) -> ToolCall:
    fn = call.get("function") or {}
    return ToolCall(
        id=call.get("id") or "call_0",
        name=fn.get("name", ""),
        arguments=_loads_args(fn.get("arguments", "")),
    )


def _parse_usage(usage: dict[str, Any]) -> TokenUsage:
    details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    # Only the numeric reasoning-token *count* is normalized (spec §12) — never any reasoning text.
    return TokenUsage(
        input_tokens=usage.get("prompt_tokens", 0),
        cached_input_tokens=details.get("cached_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
        reasoning_tokens=int(completion_details.get("reasoning_tokens", 0) or 0),
    )


def _loads_args(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {}
