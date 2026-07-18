"""OpenAI Responses API adapter (spec §12, preferred for first-party OpenAI).

The Responses API is stateful and function-calling native. History is expressed as ``input`` items:
prior ``function_call`` items plus ``function_call_output`` items carry tool results back.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ..core.errors import ErrorType
from ..core.events import ModelEventType, NormalizedModelEvent, TokenUsage
from ..core.models import ModelCapabilities, RemoteModel
from .base import (
    HealthResult,
    NormalizedModelRequest,
    Role,
    TokenEstimate,
    default_probe,
    normalized_tool_call,
    parse_model_catalog,
    rough_token_estimate,
)
from .transport import Transport, TransportError


class OpenAIResponsesAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        provider_type: str = "openai",
        extra_headers: dict[str, str] | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.provider_type = provider_type
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
            return HealthResult(ok=False, detail=exc.message)

    async def list_models(self) -> list[RemoteModel]:
        data = await self.transport.get_json("/models")
        return parse_model_catalog(data)

    async def probe_model(self, model_id: str) -> ModelCapabilities:
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
        data = await self.transport.post_json("/responses", payload)
        for event in _events_from_response(data):
            yield event

    async def _stream(self, payload: dict[str, Any]) -> AsyncIterator[NormalizedModelEvent]:
        payload["stream"] = True
        response_id: str | None = None
        async for evt in self.transport.stream_sse("/responses", payload):
            etype = evt.get("type", "")
            if etype == "response.output_text.delta" and evt.get("delta"):
                yield NormalizedModelEvent(
                    type=ModelEventType.TEXT_DELTA, text=evt["delta"], response_id=response_id
                )
            elif etype in ("response.completed", "response.incomplete"):
                response = evt.get("response", {})
                response_id = response.get("id", response_id)
                for event in _events_from_response(response, text_already_streamed=True):
                    yield event
                return
            elif etype == "response.created":
                response_id = (evt.get("response") or {}).get("id", response_id)
        yield NormalizedModelEvent(type=ModelEventType.DONE, response_id=response_id)

    # ------------------------------------------------------------------ payload

    def _build_payload(self, request: NormalizedModelRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "input": _to_responses_input(request),
            "max_output_tokens": request.max_tokens,
        }
        if request.system:
            payload["instructions"] = request.system
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                }
                for tool in request.tools
            ]
        return payload


def _to_responses_input(request: NormalizedModelRequest) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for msg in request.messages:
        role = msg.role.value if hasattr(msg.role, "value") else msg.role
        if role == Role.TOOL.value:
            items.append(
                {"type": "function_call_output", "call_id": msg.tool_call_id, "output": msg.content}
            )
        elif role == Role.ASSISTANT.value and msg.tool_calls:
            for call in msg.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    }
                )
            if msg.content:
                items.append({"role": "assistant", "content": msg.content})
        else:
            items.append({"role": role, "content": msg.content})
    return items


def _events_from_response(
    data: dict[str, Any], text_already_streamed: bool = False
) -> list[NormalizedModelEvent]:
    events: list[NormalizedModelEvent] = []
    response_id = data.get("id")
    output = data.get("output", [])
    status = str(data.get("status") or "").lower()
    incomplete = status == "incomplete"
    failed = status == "failed"
    if not text_already_streamed:
        text = _extract_text(output)
        if text:
            events.append(
                NormalizedModelEvent(
                    type=ModelEventType.TEXT_DELTA, text=text, response_id=response_id
                )
            )
    # Do not surface tool calls from a truncated/failed response — acting on a partial function
    # call is unsafe. Only a genuine completion produces tool calls.
    if not incomplete and not failed:
        for item in output:
            if item.get("type") == "function_call":
                events.append(
                    normalized_tool_call(
                        call_id=item.get("call_id") or item.get("id"),
                        name=item.get("name"),
                        arguments=item.get("arguments", ""),
                        response_id=response_id,
                    )
                )
    if data.get("usage"):
        events.append(
            NormalizedModelEvent(type=ModelEventType.USAGE, usage=_parse_usage(data["usage"]))
        )
    # An incomplete or failed response is NOT a successful completion (item 8): emit a normalized
    # error instead of DONE so the run is not counted as completed.
    if incomplete:
        events.append(_incomplete_error(data, response_id))
    elif failed:
        events.append(_failed_error(data, response_id))
    else:
        events.append(NormalizedModelEvent(type=ModelEventType.DONE, response_id=response_id))
    return events


def _incomplete_error(data: dict[str, Any], response_id: str | None) -> NormalizedModelEvent:
    reason = str((data.get("incomplete_details") or {}).get("reason") or "").lower()
    if reason in ("max_output_tokens", "max_tokens", "token_limit", "length"):
        etype = ErrorType.CONTEXT_LIMIT
    elif reason in ("content_filter", "content_filtering", "safety"):
        etype = ErrorType.CONTENT_FILTERED
    else:
        etype = ErrorType.INCOMPLETE_RESPONSE
    return NormalizedModelEvent(
        type=ModelEventType.ERROR,
        error_type=etype.value,
        response_id=response_id,
        error_message=f"response incomplete: {reason or 'unknown reason'}",
    )


def _failed_error(data: dict[str, Any], response_id: str | None) -> NormalizedModelEvent:
    err = data.get("error") or {}
    code = str(err.get("code") or "").lower()
    if "content" in code or "safety" in code:
        etype = ErrorType.CONTENT_FILTERED
    else:
        etype = ErrorType.INVALID_REQUEST
    return NormalizedModelEvent(
        type=ModelEventType.ERROR,
        error_type=etype.value,
        response_id=response_id,
        error_message=f"response failed: {err.get('message') or code or 'unknown error'}",
    )


def _extract_text(output: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in output:
        if item.get("type") == "message":
            for block in item.get("content", []):
                if block.get("type") == "output_text" and block.get("text"):
                    parts.append(block["text"])
    return "".join(parts)


def _parse_usage(usage: dict[str, Any]) -> TokenUsage:
    details = usage.get("input_tokens_details") or {}
    return TokenUsage(
        input_tokens=usage.get("input_tokens", 0),
        cached_input_tokens=details.get("cached_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
    )
