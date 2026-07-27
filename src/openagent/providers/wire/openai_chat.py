"""The OpenAI Chat Completions wire (spec §7, §11, §15, §16, §17, §18, §19).

Seven of the nine v0.2 providers speak this protocol: DeepSeek, Qwen, Kimi, GLM, MiniMax (as a
fallback), OpenRouter, and LM Studio in one of its modes. They differ in field names, parameter
bounds, how reasoning is requested and returned, and whether streamed tool arguments need an
explicit opt-in — all of which a :class:`CompatibilityProfile` already describes. So this is one
serializer configured seven ways, not seven serializers.

The parts no provider gets to differ in are enforced here rather than trusted:

* tool-argument fragments are accumulated and parsed **once**, by
  :class:`~..streaming.StreamingTurnAssembler`;
* an incomplete tool call reaches the caller as an error;
* reasoning is captured for replay and never emitted as assistant text;
* the native assistant message is preserved so a continuation can replay what the provider actually
  said, including reasoning fields a normalized transcript cannot represent (spec §10).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ...core.errors import ErrorType
from ...core.events import ModelEventType, NormalizedModelEvent
from ...core.models import Protocol
from ..base import Message, NormalizedModelRequest, Role
from ..compat.profiles_v2 import (
    AssistantHistoryPolicy,
    CompatibilityProfile,
    ReasoningRequestStyle,
)
from ..continuation import ContinuationEnvelope, ContinuationStrategy
from ..error_mapping import ProviderErrorSignal, map_provider_error
from ..streaming import AssembledTurn, StreamingTurnAssembler, Usage
from ..transport import Transport, TransportError
from .base import (
    ToolPreparation,
    interrupted,
    parse_openai_usage,
    prepare_tools,
    resolve_tool_choice,
    tool_call_events,
)

#: Overridable so a variant endpoint (LM Studio's ``/api/v0``) reuses this serializer instead of
#: copying it. The path is the only thing that differs there.
_PATH = "/chat/completions"

#: Effort values that mean "do not reason". Sent as an explicit disable rather than by omitting the
#: field, because omission means "provider default" on some endpoints and "off" on others.
_REASONING_OFF = {"off", "none", "disabled", "minimal"}


class OpenAIChatWire:
    """Serialize to, and read from, an OpenAI Chat Completions endpoint."""

    protocol = Protocol.OPENAI_CHAT
    path = _PATH

    def __init__(
        self,
        *,
        profile: CompatibilityProfile,
        transport: Transport,
        reasoning_effort: str | None = None,
        thinking_budget: int | None = None,
    ) -> None:
        self.profile = profile
        self.transport = transport
        self.reasoning_effort = reasoning_effort
        self.thinking_budget = thinking_budget
        #: The last assembled turn, available after :meth:`events` is exhausted.
        self.last_turn: AssembledTurn | None = None
        self._native_assistant_message: dict[str, Any] | None = None

    # ------------------------------------------------------------------ payload

    def build_payload(
        self,
        request: NormalizedModelRequest,
        *,
        stream: bool,
        tool_choice: str | None = "auto",
    ) -> tuple[dict[str, Any], ToolPreparation]:
        """Serialize one request. Returns the payload and what fitting the tools cost."""

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": self._messages(request),
        }
        payload[self.profile.max_tokens_field] = request.max_tokens

        temperature = self.profile.clamp_temperature(request.temperature)
        if temperature is not None:
            payload["temperature"] = temperature

        prep = prepare_tools(request.tools, self.profile, wrap=_wrap_function)
        if prep.wire_tools:
            payload["tools"] = prep.wire_tools
            choice = resolve_tool_choice(self.profile, tool_choice, prep)
            if choice is not None:
                payload["tool_choice"] = choice
            # GLM needs an explicit opt-in before it will stream tool-call arguments at all. Sent
            # only where it is meaningful: with no tools there is nothing to stream, and on a
            # non-stream request the field describes a mode that is not in use (spec §20.2).
            if stream and self.profile.tool_stream_request_field:
                payload[self.profile.tool_stream_request_field] = True

        reasoning = self._reasoning_fields()
        payload.update(reasoning)

        if stream:
            payload["stream"] = True
            if self.profile.stream_usage:
                payload["stream_options"] = {"include_usage": True}

        # Profile extras first, then removals: a profile that both adds and drops a field is
        # self-contradictory, and resolving it in favour of *not sending* is the safe direction —
        # an unsent field cannot be rejected.
        for key, value in self.profile.extra_request_fields.items():
            payload.setdefault(key, value)
        for param in self.profile.drop_params:
            payload.pop(param, None)
        return payload, prep

    def _reasoning_fields(self) -> dict[str, Any]:
        style = self.profile.reasoning_request_style
        effort = self.reasoning_effort
        if style is ReasoningRequestStyle.NONE or style is ReasoningRequestStyle.IMPLICIT:
            return {}
        if effort is None:
            return {}
        if style is ReasoningRequestStyle.REASONING_EFFORT:
            return {"reasoning": {"effort": effort}}
        if style is ReasoningRequestStyle.THINKING_OBJECT:
            enabled = effort.lower() not in _REASONING_OFF
            return {"thinking": {"type": "enabled" if enabled else "disabled"}}
        if style is ReasoningRequestStyle.THINKING_BUDGET:
            if effort.lower() in _REASONING_OFF:
                return {"thinking": {"type": "disabled"}}
            thinking: dict[str, Any] = {"type": "enabled"}
            if self.thinking_budget:
                thinking["budget_tokens"] = self.thinking_budget
            return {"thinking": thinking}
        return {}

    def _messages(self, request: NormalizedModelRequest) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for message in request.messages:
            messages.extend(self._serialize(message))
        return messages

    def _serialize(self, message: Message) -> list[dict[str, Any]]:
        role = message.role.value if hasattr(message.role, "value") else str(message.role)

        if role == Role.TOOL.value:
            if not message.tool_call_id:
                # An unkeyed tool result attaches to whichever call the provider guesses, and
                # nothing downstream can detect the mismatch (spec §10).
                raise ValueError("a tool result needs the call_id of the call it answers")
            return [
                {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
            ]

        if role == Role.ASSISTANT.value:
            if message.raw_blocks:
                # The provider's own message, replayed verbatim. This is the only shape that
                # preserves reasoning fields for the profiles that require them.
                return [block for block in message.raw_blocks if isinstance(block, dict)]
            entry: dict[str, Any] = {"role": "assistant", "content": message.content or None}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                    }
                    for call in message.tool_calls
                ]
            return [entry]

        return [{"role": role, "content": message.content}]

    # ------------------------------------------------------------------ execution

    async def events(
        self, request: NormalizedModelRequest, *, tool_choice: str | None = "auto"
    ) -> AsyncIterator[NormalizedModelEvent]:
        """Run one turn and yield normalized events.

        Text arrives as it streams; tool calls, usage and the terminal event come from the assembled
        turn, because a tool call is only knowable once its arguments are complete.
        """

        self.last_turn = None
        self._native_assistant_message = None
        try:
            payload, prep = self.build_payload(
                request, stream=bool(request.stream), tool_choice=tool_choice
            )
        except ValueError as exc:
            yield NormalizedModelEvent(
                type=ModelEventType.ERROR,
                error_type=ErrorType.TOOL_HISTORY_INCOMPLETE.value,
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
                error_type=self._map_error(exc).value,
                error_message=exc.message,
            )

    def _record_response_extras(self, data: dict[str, Any]) -> None:
        """Hook for a variant endpoint that returns more than the standard body.

        Exists so LM Studio can read its ``stats``/``model_info`` from the response the base class
        already parsed, rather than re-issuing the request or forking this serializer.
        """

    def _map_error(self, exc: TransportError) -> ErrorType:
        """Refine a transport failure with the provider's own payload when it sent one.

        ``exc.body`` is populated for in-stream error events, which the transport raises before this
        wire reads them — without it those failures would all flatten to the status classification.
        """

        return map_provider_error(
            self.profile.error_mapper,
            ProviderErrorSignal(
                status=exc.status, message=exc.message, base=exc.error_type, body=exc.body
            ),
        )

    async def _complete(self, payload: dict[str, Any]) -> AsyncIterator[NormalizedModelEvent]:
        data = await self.transport.post_json(self.path, payload)
        response_id = _text_or_none(data.get("id"))
        self._record_response_extras(data)

        # Some endpoints report application-level failures with HTTP 200 and a code in the body
        # (MiniMax). Read before anything else: an empty completion with an error code is a failure,
        # and treating it as a successful empty turn is how a billing error becomes a silent no-op.
        body_error = map_provider_error(
            self.profile.error_mapper,
            ProviderErrorSignal(status=200, message="", base=ErrorType.UNKNOWN, body=data),
        )
        if body_error is not ErrorType.UNKNOWN:
            yield NormalizedModelEvent(
                type=ModelEventType.ERROR,
                error_type=body_error.value,
                error_message=_body_error_message(data),
                response_id=response_id,
            )
            return

        choices = data.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else {}
        choice = choice if isinstance(choice, dict) else {}
        message = choice.get("message")
        message = message if isinstance(message, dict) else {}

        assembler = StreamingTurnAssembler()
        text = message.get("content")
        if isinstance(text, str) and text:
            assembler.append_text(text)
            yield NormalizedModelEvent(
                type=ModelEventType.TEXT_DELTA, text=text, response_id=response_id
            )
        assembler.append_reasoning(self._reasoning_of(message))

        for index, call in enumerate(_list(message.get("tool_calls"))):
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            assembler.register_tool_call(
                index=index,
                tool_id=_text_or_none(call.get("id")),
                name=_text_or_none(function.get("name")),
            )
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                assembler.append_tool_argument_fragment(arguments, index=index)
            elif isinstance(arguments, dict):
                # A few endpoints return arguments already parsed. Re-serializing keeps one parse
                # path rather than two ways for a call to become "complete".
                assembler.append_tool_argument_fragment(json.dumps(arguments), index=index)

        assembler.set_usage(_usage(data.get(self.profile.usage_location)))
        assembler.set_finish_reason(_text_or_none(choice.get("finish_reason")))

        turn = assembler.build()
        self.last_turn = _with_response_id(turn, response_id)
        # The provider's own message, kept verbatim — the only form that carries reasoning fields
        # and provider-specific extras a reconstruction would lose.
        self._native_assistant_message = dict(message) if message else None

        for event in tool_call_events(turn, response_id=response_id):
            yield event
        if turn.usage is not None:
            yield NormalizedModelEvent(
                type=ModelEventType.USAGE,
                usage=_token_usage(turn.usage),
                response_id=response_id,
            )
        yield NormalizedModelEvent(type=ModelEventType.DONE, response_id=response_id)

    async def _stream(self, payload: dict[str, Any]) -> AsyncIterator[NormalizedModelEvent]:
        assembler = StreamingTurnAssembler()
        response_id: str | None = None
        saw_finish = False

        async for chunk in self.transport.stream_sse(self.path, payload):
            response_id = _text_or_none(chunk.get("id")) or response_id
            usage = _usage(chunk.get(self.profile.usage_location))
            if usage is not None:
                assembler.set_usage(usage)

            for choice in _list(chunk.get("choices")):
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}

                content = delta.get("content")
                if isinstance(content, str) and content:
                    assembler.append_text(content)
                    yield NormalizedModelEvent(
                        type=ModelEventType.TEXT_DELTA, text=content, response_id=response_id
                    )

                assembler.append_reasoning(self._reasoning_of(delta))

                for call in _list(delta.get("tool_calls")):
                    if not isinstance(call, dict):
                        continue
                    index = call.get("index")
                    index = (
                        index if isinstance(index, int) and not isinstance(index, bool) else None
                    )
                    tool_id = _text_or_none(call.get("id"))
                    function = (
                        call.get("function") if isinstance(call.get("function"), dict) else {}
                    )
                    name = _text_or_none(function.get("name"))
                    if name is not None:
                        assembler.append_tool_name(name, index=index, tool_id=tool_id)
                    elif tool_id is not None or index is not None:
                        assembler.register_tool_call(index=index, tool_id=tool_id)
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        assembler.append_tool_argument_fragment(
                            arguments, index=index, tool_id=tool_id
                        )

                finish = _text_or_none(choice.get("finish_reason"))
                if finish is not None:
                    assembler.set_finish_reason(finish)
                    saw_finish = True

        if not saw_finish:
            # The transport stayed up and the provider stopped without saying it was done. Recorded
            # as interrupted so a truncated turn is not mistaken for a complete short one.
            assembler.mark_interrupted()

        turn = assembler.build()
        self.last_turn = _with_response_id(turn, response_id)
        self._native_assistant_message = _reconstruct_native_message(turn, self.profile)

        for event in tool_call_events(turn, response_id=response_id):
            yield event
        if turn.usage is not None:
            yield NormalizedModelEvent(
                type=ModelEventType.USAGE,
                usage=_token_usage(turn.usage),
                response_id=response_id,
            )
        if interrupted(turn) and not turn.tool_calls:
            yield NormalizedModelEvent(
                type=ModelEventType.ERROR,
                error_type=ErrorType.STREAM_INTERRUPTED.value,
                error_message="the provider stopped streaming without a terminal event",
                response_id=response_id,
            )
            return
        yield NormalizedModelEvent(type=ModelEventType.DONE, response_id=response_id)

    def _reasoning_of(self, container: dict[str, Any]) -> str | None:
        """Read reasoning from the field *this profile* names, and nowhere else.

        Scanning for any plausible reasoning key would pick up a field some future endpoint uses for
        something else, and the cost of being wrong is reasoning text in the wrong place.
        """

        field = self.profile.reasoning_response_field
        if not field:
            return None
        value = container.get(field)
        return value if isinstance(value, str) and value else None

    # ------------------------------------------------------------------ continuation

    def build_continuation(self, *, model_id: str | None = None) -> ContinuationEnvelope:
        """Describe how the turn just run can be continued (spec §10).

        Native replay is chosen only when the profile actually needs it *and* there is native
        material to replay. Preferring it unconditionally would store a provider message for
        endpoints where normalized history is equivalent, which is a larger artifact for no gain.
        """

        needs_native = self.profile.assistant_history_policy in {
            AssistantHistoryPolicy.NATIVE_MESSAGE,
            AssistantHistoryPolicy.NATIVE_MESSAGE_WITH_REASONING,
        }
        if needs_native and self._native_assistant_message:
            return ContinuationEnvelope.build(
                provider_type=self.profile.provider_type,
                protocol=self.protocol,
                strategy=ContinuationStrategy.NATIVE_MESSAGE_REPLAY,
                native_assistant_message=self._native_assistant_message,
                model_id=model_id,
            )
        return ContinuationEnvelope.build(
            provider_type=self.profile.provider_type,
            protocol=self.protocol,
            strategy=ContinuationStrategy.NORMALIZED_HISTORY,
            model_id=model_id,
        )

    @property
    def native_assistant_message(self) -> dict[str, Any] | None:
        return self._native_assistant_message


# ------------------------------------------------------------------------------- helpers


def _wrap_function(schema: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": schema}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _usage(raw: object) -> Usage | None:
    parsed = parse_openai_usage(raw)
    if parsed is None:
        return None
    if not any(
        (
            parsed.input_tokens,
            parsed.output_tokens,
            parsed.reasoning_tokens,
            parsed.cached_input_tokens,
        )
    ):
        return None
    return Usage(
        input_tokens=parsed.input_tokens,
        output_tokens=parsed.output_tokens,
        reasoning_tokens=parsed.reasoning_tokens,
    )


def _token_usage(usage: Usage):
    from ...core.events import TokenUsage

    return TokenUsage(
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        reasoning_tokens=usage.reasoning_tokens or 0,
    )


def _with_response_id(turn: AssembledTurn, response_id: str | None) -> AssembledTurn:
    """Attach the provider's response id to the turn.

    :class:`AssembledTurn` is protocol-agnostic and frozen, so the id rides in a subclass-free way:
    a shallow replace keeps the value semantics the assembler relies on.
    """

    return _TurnWithId(
        text=turn.text,
        reasoning=turn.reasoning,
        tool_calls=turn.tool_calls,
        usage=turn.usage,
        finish_reason=turn.finish_reason,
        truncated=turn.truncated,
        response_id=response_id,
    )


from dataclasses import dataclass  # noqa: E402 - used only by the subclass below


@dataclass(frozen=True)
class _TurnWithId(AssembledTurn):
    """An assembled turn plus the provider's response id."""

    response_id: str | None = None


def _reconstruct_native_message(
    turn: AssembledTurn, profile: CompatibilityProfile
) -> dict[str, Any] | None:
    """Rebuild the assistant message a streamed turn implies.

    A streamed turn never had a message object to keep, so replay needs one built from the
    fragments. Reasoning is included only when the profile names a field for it — inventing a key
    name would produce a request the endpoint rejects, which is worse than a weaker continuation.
    """

    if not turn.text and not turn.tool_calls and not turn.reasoning:
        return None
    message: dict[str, Any] = {"role": "assistant", "content": turn.text or None}
    if turn.reasoning and profile.reasoning_response_field:
        message[profile.reasoning_response_field] = turn.reasoning
    complete = [call for call in turn.tool_calls if call.complete and call.id and call.name]
    if complete:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.raw_arguments or "{}"},
            }
            for call in complete
        ]
    return message


def _body_error_message(data: dict[str, Any]) -> str:
    base_resp = data.get("base_resp")
    if isinstance(base_resp, dict):
        message = base_resp.get("status_msg")
        if isinstance(message, str) and message:
            return message
        return f"provider reported status_code {base_resp.get('status_code')}"
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return "provider reported an error in the response body"
