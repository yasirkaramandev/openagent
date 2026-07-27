"""The OpenAI Responses wire (spec §7, §13.3, §16.3).

The reason this protocol matters to v0.2 is not its request shape — it is ``previous_response_id``.
Responses can hold the conversation *server-side*, which is the only continuation strategy that
preserves everything the model saw without OpenAgent storing any of it. LM Studio supports it locally
and Qwen supports it in some regions.

That makes it a privacy decision as much as a transport one, so the wire treats server-side state as
opt-in: ``store`` defaults to ``False`` and sending a ``previous_response_id`` while ``store`` is off
is refused locally rather than turned into a confusing provider-side error about an unknown response.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ...core.errors import ErrorType
from ...core.events import ModelEventType, NormalizedModelEvent, TokenUsage
from ...core.models import Protocol
from ..base import Message, NormalizedModelRequest, Role
from ..compat.profiles_v2 import CompatibilityProfile, ReasoningRequestStyle
from ..continuation import ContinuationEnvelope, ContinuationStrategy
from ..error_mapping import ProviderErrorSignal, map_provider_error
from ..streaming import AssembledTurn, StreamingTurnAssembler, Usage
from ..transport import Transport, TransportError
from .base import ToolPreparation, interrupted, prepare_tools, tool_call_events

_PATH = "/responses"

_REASONING_OFF = {"off", "none", "disabled"}


class OpenAIResponsesWire:
    """Serialize to, and read from, an OpenAI Responses endpoint."""

    protocol = Protocol.OPENAI_RESPONSES

    def __init__(
        self,
        *,
        profile: CompatibilityProfile,
        transport: Transport,
        reasoning_effort: str | None = None,
        store: bool = False,
        previous_response_id: str | None = None,
    ) -> None:
        self.profile = profile
        self.transport = transport
        self.reasoning_effort = reasoning_effort
        #: Whether the *provider* retains this conversation. Off by default (spec §13.3).
        self.store = store
        self.previous_response_id = previous_response_id
        self.last_turn: AssembledTurn | None = None
        self._last_response_id: str | None = None

    # ------------------------------------------------------------------ payload

    def build_payload(
        self,
        request: NormalizedModelRequest,
        *,
        stream: bool,
        tool_choice: str | None = "auto",
    ) -> tuple[dict[str, Any], ToolPreparation]:
        payload: dict[str, Any] = {
            "model": request.model,
            "input": self._input(request.messages),
            "max_output_tokens": request.max_tokens,
            "store": self.store,
        }
        if request.system:
            payload["instructions"] = request.system

        temperature = self.profile.clamp_temperature(request.temperature)
        if temperature is not None:
            payload["temperature"] = temperature

        prep = prepare_tools(request.tools, self.profile, wrap=_wrap_tool)
        if prep.wire_tools:
            payload["tools"] = prep.wire_tools
            resolved = self.profile.normalize_tool_choice(tool_choice)
            if tool_choice == "required" and resolved != "required":
                prep.tool_choice_downgraded = True
            if resolved is not None:
                payload["tool_choice"] = "required" if resolved == "required" else resolved

        if (
            self.reasoning_effort
            and self.profile.reasoning_request_style is ReasoningRequestStyle.REASONING_EFFORT
            and self.reasoning_effort.lower() not in _REASONING_OFF
        ):
            payload["reasoning"] = {"effort": self.reasoning_effort}

        if self.previous_response_id:
            if not self.store:
                # The provider can only look up a response it was asked to keep. Refusing here names
                # the real cause — a local setting — instead of surfacing "unknown response id".
                raise ValueError(
                    "previous_response_id requires store=True; with local-only state, resume by "
                    "replaying the recorded history instead"
                )
            payload["previous_response_id"] = self.previous_response_id

        if stream:
            payload["stream"] = True
        for key, value in self.profile.extra_request_fields.items():
            payload.setdefault(key, value)
        for param in self.profile.drop_params:
            payload.pop(param, None)
        return payload, prep

    def _input(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Responses input items. Tool results are ``function_call_output`` items keyed by call id."""

        items: list[dict[str, Any]] = []
        for message in messages:
            role = message.role.value if hasattr(message.role, "value") else str(message.role)

            if role == Role.TOOL.value:
                if not message.tool_call_id:
                    raise ValueError("a tool result needs the call_id of the call it answers")
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": message.content or "",
                    }
                )
                continue

            if role == Role.ASSISTANT.value:
                if message.raw_blocks:
                    items.extend(block for block in message.raw_blocks if isinstance(block, dict))
                    continue
                for call in message.tool_calls:
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": call.id,
                            "name": call.name,
                            "arguments": json.dumps(call.arguments),
                        }
                    )
                if message.content:
                    items.append(
                        {
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": message.content}],
                        }
                    )
                continue

            if message.content:
                items.append(
                    {"role": "user", "content": [{"type": "input_text", "text": message.content}]}
                )
        return items

    # ------------------------------------------------------------------ execution

    async def events(
        self, request: NormalizedModelRequest, *, tool_choice: str | None = "auto"
    ) -> AsyncIterator[NormalizedModelEvent]:
        self.last_turn = None
        self._last_response_id = None
        try:
            payload, prep = self.build_payload(
                request, stream=bool(request.stream), tool_choice=tool_choice
            )
        except ValueError as exc:
            yield NormalizedModelEvent(
                type=ModelEventType.ERROR,
                error_type=ErrorType.INVALID_REQUEST.value,
                error_message=str(exc),
            )
            return

        for name in prep.rejected:
            yield NormalizedModelEvent(
                type=ModelEventType.ERROR,
                error_type=ErrorType.INVALID_TOOL_CALL.value,
                error_message=f"tool {name!r} could not be expressed for this endpoint and was not sent",
            )

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
                error_type=map_provider_error(
                    self.profile.error_mapper,
                    ProviderErrorSignal(
                        status=exc.status,
                        message=exc.message,
                        base=exc.error_type,
                        body=exc.body,
                    ),
                ).value,
                error_message=exc.message,
            )

    async def _complete(self, payload: dict[str, Any]) -> AsyncIterator[NormalizedModelEvent]:
        data = await self.transport.post_json(_PATH, payload)
        self._last_response_id = _text_or_none(data.get("id"))
        assembler = StreamingTurnAssembler()

        tool_index = 0
        for item in _list(data.get("output")):
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "message":
                for part in _list(item.get("content")):
                    if not isinstance(part, dict):
                        continue
                    text = part.get("text")
                    if part.get("type") == "output_text" and isinstance(text, str) and text:
                        assembler.append_text(text)
                        yield NormalizedModelEvent(
                            type=ModelEventType.TEXT_DELTA,
                            text=text,
                            response_id=self._last_response_id,
                        )
            elif kind == "function_call":
                assembler.register_tool_call(
                    index=tool_index,
                    tool_id=_text_or_none(item.get("call_id")) or _text_or_none(item.get("id")),
                    name=_text_or_none(item.get("name")),
                )
                arguments = item.get("arguments")
                if isinstance(arguments, str):
                    assembler.append_tool_argument_fragment(arguments, index=tool_index)
                elif isinstance(arguments, dict):
                    assembler.append_tool_argument_fragment(json.dumps(arguments), index=tool_index)
                tool_index += 1
            elif kind == "reasoning":
                # Summary text only; Responses does not return raw chain-of-thought. Accumulated for
                # replay, never emitted.
                for part in _list(item.get("summary")):
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        assembler.append_reasoning(part["text"])

        assembler.set_usage(_usage(data.get("usage")))
        assembler.set_finish_reason(_finish_from_status(data))
        turn = assembler.build()
        self.last_turn = _TurnWithId(
            text=turn.text,
            reasoning=turn.reasoning,
            tool_calls=turn.tool_calls,
            usage=turn.usage,
            finish_reason=turn.finish_reason,
            truncated=turn.truncated,
            response_id=self._last_response_id,
        )

        for tool_event in tool_call_events(turn, response_id=self._last_response_id):
            yield tool_event
        usage_event = _usage_event(data.get("usage"), self._last_response_id)
        if usage_event is not None:
            yield usage_event
        yield NormalizedModelEvent(type=ModelEventType.DONE, response_id=self._last_response_id)

    async def _stream(self, payload: dict[str, Any]) -> AsyncIterator[NormalizedModelEvent]:
        assembler = StreamingTurnAssembler()
        saw_terminal = False
        raw_usage: dict[str, Any] = {}
        #: Output index → assembler tool index, so a message item between two calls does not shift
        #: the numbering.
        tool_indexes: dict[int, int] = {}

        async for event in self.transport.stream_sse(_PATH, payload):
            kind = str(event.get("type") or "")

            if kind in {"response.created", "response.in_progress"}:
                response = _dict(event.get("response"))
                self._last_response_id = _text_or_none(response.get("id")) or self._last_response_id
                continue

            if kind == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    assembler.append_text(delta)
                    yield NormalizedModelEvent(
                        type=ModelEventType.TEXT_DELTA,
                        text=delta,
                        response_id=self._last_response_id,
                    )
                continue

            if kind == "response.output_item.added":
                item = _dict(event.get("item"))
                if item.get("type") == "function_call":
                    index = _index(event)
                    tool_index = len(tool_indexes)
                    tool_indexes[index] = tool_index
                    assembler.register_tool_call(
                        index=tool_index,
                        tool_id=_text_or_none(item.get("call_id")) or _text_or_none(item.get("id")),
                        name=_text_or_none(item.get("name")),
                    )
                continue

            if kind == "response.function_call_arguments.delta":
                index = _index(event)
                existing = tool_indexes.get(index)
                tool_index = len(tool_indexes) if existing is None else existing
                tool_indexes[index] = tool_index
                delta = event.get("delta")
                if isinstance(delta, str):
                    assembler.append_tool_argument_fragment(delta, index=tool_index)
                continue

            if kind == "response.reasoning_summary_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str):
                    assembler.append_reasoning(delta)
                continue

            if kind in {"response.completed", "response.incomplete"}:
                response = _dict(event.get("response"))
                self._last_response_id = _text_or_none(response.get("id")) or self._last_response_id
                raw_usage.update(_dict(response.get("usage")))
                assembler.set_finish_reason(_finish_from_status(response))
                saw_terminal = True
                continue

            if kind in {"response.failed", "error"}:
                failed = _dict(event.get("response"))
                error = _dict(failed.get("error")) or _dict(event.get("error"))
                yield NormalizedModelEvent(
                    type=ModelEventType.ERROR,
                    error_type=ErrorType.UNKNOWN.value,
                    error_message=str(error.get("message") or "provider reported an error"),
                    response_id=self._last_response_id,
                )
                return

        if not saw_terminal:
            assembler.mark_interrupted()

        assembler.set_usage(_usage(raw_usage))
        turn = assembler.build()
        self.last_turn = _TurnWithId(
            text=turn.text,
            reasoning=turn.reasoning,
            tool_calls=turn.tool_calls,
            usage=turn.usage,
            finish_reason=turn.finish_reason,
            truncated=turn.truncated,
            response_id=self._last_response_id,
        )

        for tool_event in tool_call_events(turn, response_id=self._last_response_id):
            yield tool_event
        usage_event = _usage_event(raw_usage, self._last_response_id)
        if usage_event is not None:
            yield usage_event
        if interrupted(turn) and not turn.tool_calls:
            yield NormalizedModelEvent(
                type=ModelEventType.ERROR,
                error_type=ErrorType.STREAM_INTERRUPTED.value,
                error_message="the provider stopped streaming without a terminal event",
                response_id=self._last_response_id,
            )
            return
        yield NormalizedModelEvent(type=ModelEventType.DONE, response_id=self._last_response_id)

    # ------------------------------------------------------------------ continuation

    def build_continuation(self, *, model_id: str | None = None) -> ContinuationEnvelope:
        """Server-side state when it is genuinely available, normalized history otherwise.

        ``REMOTE_ID`` requires both that the provider was asked to keep the response *and* that it
        returned an id. Recording a remote id the provider did not retain produces a resume that
        fails at the moment the user tries it.
        """

        if self.store and self._last_response_id and self.profile.supports_server_state:
            return ContinuationEnvelope.build(
                provider_type=self.profile.provider_type,
                protocol=self.protocol,
                strategy=ContinuationStrategy.REMOTE_ID,
                remote_interaction_id=self._last_response_id,
                opaque_fields={
                    "id_field": self.profile.server_state_id_field or "previous_response_id"
                },
                model_id=model_id,
            )
        return ContinuationEnvelope.build(
            provider_type=self.profile.provider_type,
            protocol=self.protocol,
            strategy=ContinuationStrategy.NORMALIZED_HISTORY,
            model_id=model_id,
        )

    @property
    def last_response_id(self) -> str | None:
        return self._last_response_id


# ------------------------------------------------------------------------------- helpers


def _wrap_tool(schema: dict[str, Any]) -> dict[str, Any]:
    """Responses puts function tools flat, with ``type: "function"`` alongside the fields."""

    return {
        "type": "function",
        "name": schema["name"],
        "description": schema.get("description", "") or "",
        "parameters": schema.get("parameters") or {"type": "object", "properties": {}},
    }


def _finish_from_status(response: dict[str, Any]) -> str | None:
    status = response.get("status")
    if status == "completed":
        return "stop"
    if status == "incomplete":
        details = _dict(response.get("incomplete_details"))
        reason = details.get("reason")
        return "length" if reason == "max_output_tokens" else "interrupted"
    return None


def _usage(raw: object) -> Usage | None:
    if not isinstance(raw, dict) or not raw:
        return None
    input_tokens = _count(raw.get("input_tokens"))
    output_tokens = _count(raw.get("output_tokens"))
    if not input_tokens and not output_tokens:
        return None
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens)


def _usage_event(raw: object, response_id: str | None) -> NormalizedModelEvent | None:
    if not isinstance(raw, dict) or not raw:
        return None
    details = _dict(raw.get("input_tokens_details"))
    output_details = _dict(raw.get("output_tokens_details"))
    usage = TokenUsage(
        input_tokens=_count(raw.get("input_tokens")),
        cached_input_tokens=_count(details.get("cached_tokens")),
        output_tokens=_count(raw.get("output_tokens")),
        reasoning_tokens=_count(output_details.get("reasoning_tokens")),
    )
    if not any(
        (usage.input_tokens, usage.output_tokens, usage.cached_input_tokens, usage.reasoning_tokens)
    ):
        return None
    return NormalizedModelEvent(type=ModelEventType.USAGE, usage=usage, response_id=response_id)


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _index(event: dict[str, Any]) -> int:
    index = event.get("output_index")
    if not isinstance(index, int) or isinstance(index, bool):
        index = event.get("index")
    return index if isinstance(index, int) and not isinstance(index, bool) else 0


from dataclasses import dataclass  # noqa: E402 - used only by the subclass below


@dataclass(frozen=True)
class _TurnWithId(AssembledTurn):
    response_id: str | None = None
