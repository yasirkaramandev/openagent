"""The Anthropic Messages wire (spec §7, §19).

MiniMax's first-class protocol, and one of the shapes Qwen and LM Studio can be configured to speak.

Two things distinguish it from the chat wire, and both are the reason it is a separate serializer
rather than a flag on that one.

**A turn is an ordered list of typed blocks.** Text, thinking, and tool_use interleave, and the order
is meaningful. A continuation must replay the whole list — a thinking block replayed without its
``signature``, or reordered, is rejected outright by the provider. So the block list is captured
verbatim on the non-stream path and rebuilt block-for-block on the stream path, rather than being
flattened into text plus calls and reassembled from a shape that lost the order.

**A tool result is a content block inside a user message.** Two consecutive user messages is a shape
the API rejects, so consecutive tool results merge into one message here rather than at each call
site — which is where that bug lives when it is not centralized.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ...core.errors import ErrorType, classify_http_status
from ...core.events import ModelEventType, NormalizedModelEvent, TokenUsage
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
from .base import ToolPreparation, interrupted, interruption_event, prepare_tools, tool_call_events

_PATH = "/v1/messages"

_REASONING_OFF = {"off", "none", "disabled", "minimal"}

#: Anthropic's own tool_choice vocabulary. ``any`` is what "required" means here.
_CHOICE = {"auto": "auto", "required": "any", "none": "none"}

#: Effort → thinking budget. Only used when the profile asks for a budget style; a budget is
#: required by the API when thinking is enabled, so a style without one cannot be sent.
_EFFORT_BUDGET = {"low": 1024, "medium": 4096, "high": 16384}


class AnthropicMessagesWire:
    """Serialize to, and read from, an Anthropic Messages endpoint."""

    protocol = Protocol.ANTHROPIC_MESSAGES

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
        self.last_turn: AssembledTurn | None = None
        self._native_blocks: list[dict[str, Any]] = []

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
            # Required by the API, so always present rather than conditional.
            "max_tokens": request.max_tokens,
            "messages": self._messages(request.messages),
        }
        if request.system:
            payload["system"] = request.system

        temperature = self.profile.clamp_temperature(request.temperature)
        if temperature is not None:
            payload["temperature"] = temperature

        prep = prepare_tools(request.tools, self.profile, wrap=_wrap_tool)
        if prep.wire_tools:
            payload["tools"] = prep.wire_tools
            resolved = self.profile.normalize_tool_choice(tool_choice)
            if tool_choice == "required" and resolved != "required":
                prep.tool_choice_downgraded = True
            mapped = _CHOICE.get(resolved or "")
            if mapped is not None:
                payload["tool_choice"] = {"type": mapped}

        thinking = self._thinking_field()
        if thinking is not None:
            payload["thinking"] = thinking

        if stream:
            payload["stream"] = True

        for key, value in self.profile.extra_request_fields.items():
            payload.setdefault(key, value)
        for param in self.profile.drop_params:
            payload.pop(param, None)
        return payload, prep

    def _thinking_field(self) -> dict[str, Any] | None:
        style = self.profile.reasoning_request_style
        effort = self.reasoning_effort
        if effort is None or style in {ReasoningRequestStyle.NONE, ReasoningRequestStyle.IMPLICIT}:
            return None
        if effort.lower() in _REASONING_OFF:
            return {"type": "disabled"}
        if style is ReasoningRequestStyle.THINKING_OBJECT:
            return {"type": "enabled"}
        if style in {ReasoningRequestStyle.THINKING_BUDGET, ReasoningRequestStyle.REASONING_EFFORT}:
            budget = self.thinking_budget or _EFFORT_BUDGET.get(effort.lower(), 4096)
            return {"type": "enabled", "budget_tokens": budget}
        return None

    def _messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Serialize history, merging consecutive tool results into one user message."""

        out: list[dict[str, Any]] = []
        for message in messages:
            role = message.role.value if hasattr(message.role, "value") else str(message.role)

            if role == Role.TOOL.value:
                if not message.tool_call_id:
                    raise ValueError("a tool result needs the tool_use_id of the call it answers")
                block = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content or "",
                }
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
                continue

            if role == Role.ASSISTANT.value:
                if message.raw_blocks:
                    out.append({"role": "assistant", "content": list(message.raw_blocks)})
                    continue
                blocks: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                for call in message.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": call.arguments,
                        }
                    )
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
                continue

            out.append({"role": "user", "content": message.content})
        return out

    # ------------------------------------------------------------------ execution

    async def events(
        self, request: NormalizedModelRequest, *, tool_choice: str | None = "auto"
    ) -> AsyncIterator[NormalizedModelEvent]:
        self.last_turn = None
        self._native_blocks = []
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
                error_type=self._classify(exc).value,
                error_message=exc.message,
            )

    def _classify(self, exc: TransportError) -> ErrorType:
        """Prefer the provider's own error type over the status classification.

        An in-stream ``{"error": {"type": "overloaded_error"}}`` is raised by the transport before
        this wire reads the stream, so the payload it carries is the only place that type survives.
        """

        base = exc.error_type
        error_object = _dict((exc.body or {}).get("error"))
        if error_object:
            refined = _stream_error(error_object)
            if refined is not ErrorType.UNKNOWN:
                base = refined
        return map_provider_error(
            self.profile.error_mapper,
            ProviderErrorSignal(status=exc.status, message=exc.message, base=base, body=exc.body),
        )

    async def _complete(self, payload: dict[str, Any]) -> AsyncIterator[NormalizedModelEvent]:
        data = await self.transport.post_json(_PATH, payload)
        response_id = _text_or_none(data.get("id"))
        assembler = StreamingTurnAssembler()

        blocks = [block for block in _list(data.get("content")) if isinstance(block, dict)]
        # Kept verbatim, order included: this is exactly the list a continuation replays.
        self._native_blocks = blocks

        tool_index = 0
        for block in blocks:
            kind = block.get("type")
            if kind == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    assembler.append_text(text)
                    yield NormalizedModelEvent(
                        type=ModelEventType.TEXT_DELTA, text=text, response_id=response_id
                    )
            elif kind == "thinking":
                thinking = block.get("thinking")
                if isinstance(thinking, str):
                    assembler.append_reasoning(thinking)
            elif kind == "tool_use":
                assembler.register_tool_call(
                    index=tool_index,
                    tool_id=_text_or_none(block.get("id")),
                    name=_text_or_none(block.get("name")),
                )
                arguments = block.get("input")
                if isinstance(arguments, dict):
                    import json

                    assembler.append_tool_argument_fragment(json.dumps(arguments), index=tool_index)
                tool_index += 1

        assembler.set_usage(_usage(data.get("usage")))
        assembler.set_finish_reason(_text_or_none(data.get("stop_reason")))
        turn = assembler.build()
        self.last_turn = _TurnWithId(
            text=turn.text,
            reasoning=turn.reasoning,
            tool_calls=turn.tool_calls,
            usage=turn.usage,
            finish_reason=turn.finish_reason,
            truncated=turn.truncated,
            response_id=response_id,
        )

        for tool_event in tool_call_events(turn, response_id=response_id):
            yield tool_event
        usage_event = _usage_event(data.get("usage"), response_id)
        if usage_event is not None:
            yield usage_event
        yield NormalizedModelEvent(type=ModelEventType.DONE, response_id=response_id)

    async def _stream(self, payload: dict[str, Any]) -> AsyncIterator[NormalizedModelEvent]:
        assembler = StreamingTurnAssembler()
        response_id: str | None = None
        saw_stop = False
        raw_usage: dict[str, Any] = {}
        #: Blocks under construction, by stream index. Rebuilt as the provider sent them so the
        #: replayed list is block-for-block identical, signatures included.
        blocks: dict[int, dict[str, Any]] = {}
        #: Stream index → assembler tool index, so a text block between two tool blocks does not
        #: shift the tool numbering.
        tool_indexes: dict[int, int] = {}

        async for event in self.transport.stream_sse(_PATH, payload):
            kind = event.get("type")

            if kind == "message_start":
                message = _dict(event.get("message"))
                response_id = _text_or_none(message.get("id")) or response_id
                if message.get("usage"):
                    raw_usage.update(_dict(message.get("usage")))
                continue

            if kind == "content_block_start":
                index = _index(event)
                block = dict(_dict(event.get("content_block")))
                blocks[index] = block
                if block.get("type") == "tool_use":
                    tool_index = len(tool_indexes)
                    tool_indexes[index] = tool_index
                    assembler.register_tool_call(
                        index=tool_index,
                        tool_id=_text_or_none(block.get("id")),
                        name=_text_or_none(block.get("name")),
                    )
                continue

            if kind == "content_block_delta":
                index = _index(event)
                delta = _dict(event.get("delta"))
                block = blocks.setdefault(index, {})
                delta_type = delta.get("type")

                if delta_type == "text_delta":
                    text = delta.get("text")
                    if isinstance(text, str) and text:
                        block.setdefault("type", "text")
                        block["text"] = str(block.get("text") or "") + text
                        assembler.append_text(text)
                        yield NormalizedModelEvent(
                            type=ModelEventType.TEXT_DELTA, text=text, response_id=response_id
                        )
                elif delta_type == "thinking_delta":
                    thinking = delta.get("thinking")
                    if isinstance(thinking, str) and thinking:
                        block.setdefault("type", "thinking")
                        block["thinking"] = str(block.get("thinking") or "") + thinking
                        # Accumulated for replay, never emitted: no consumer renders reasoning
                        # today, and making this one provider the exception would surface thoughts
                        # the moment some future renderer reads the field.
                        assembler.append_reasoning(thinking)
                elif delta_type == "signature_delta":
                    signature = delta.get("signature")
                    if isinstance(signature, str) and signature:
                        # Opaque and mandatory for replay. A thinking block sent back without it is
                        # rejected, which is why it is stored on the block and not merely counted.
                        block["signature"] = str(block.get("signature") or "") + signature
                elif delta_type == "input_json_delta":
                    fragment = delta.get("partial_json")
                    if isinstance(fragment, str):
                        block.setdefault("type", "tool_use")
                        block["_partial_json"] = str(block.get("_partial_json") or "") + fragment
                        existing = tool_indexes.get(index)
                        tool_index = len(tool_indexes) if existing is None else existing
                        tool_indexes[index] = tool_index
                        assembler.append_tool_argument_fragment(fragment, index=tool_index)
                continue

            if kind == "content_block_stop":
                continue

            if kind == "message_delta":
                delta = _dict(event.get("delta"))
                stop = _text_or_none(delta.get("stop_reason"))
                if stop is not None:
                    assembler.set_finish_reason(stop)
                    saw_stop = True
                if event.get("usage"):
                    raw_usage.update(_dict(event.get("usage")))
                continue

            if kind == "message_stop":
                break

            if kind == "error":
                error = _dict(event.get("error"))
                yield NormalizedModelEvent(
                    type=ModelEventType.ERROR,
                    error_type=_stream_error(error).value,
                    error_message=str(error.get("message") or "provider reported an error"),
                    response_id=response_id,
                )
                return

            # "ping" and any future event name: ignored rather than treated as malformed. An
            # unrecognised keep-alive must not end a turn.

        if not saw_stop:
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
            response_id=response_id,
        )
        self._native_blocks = _finalize_blocks(blocks)

        for tool_event in tool_call_events(turn, response_id=response_id):
            yield tool_event
        usage_event = _usage_event(raw_usage, response_id)
        if usage_event is not None:
            yield usage_event
        if interrupted(turn):
            yield interruption_event(turn, response_id=response_id)
            return
        yield NormalizedModelEvent(type=ModelEventType.DONE, response_id=response_id)

    # ------------------------------------------------------------------ continuation

    def build_continuation(self, *, model_id: str | None = None) -> ContinuationEnvelope:
        native_wanted = self.profile.assistant_history_policy in {
            AssistantHistoryPolicy.NATIVE_MESSAGE,
            AssistantHistoryPolicy.NATIVE_MESSAGE_WITH_REASONING,
        }
        # A thinking block is only replayable with its signature, so a turn that reasoned needs
        # native replay regardless of what the profile prefers for plain turns.
        has_signed_thinking = any(
            block.get("type") == "thinking" and block.get("signature")
            for block in self._native_blocks
        )
        if (native_wanted or has_signed_thinking) and self._native_blocks:
            return ContinuationEnvelope.build(
                provider_type=self.profile.provider_type,
                protocol=self.protocol,
                strategy=ContinuationStrategy.NATIVE_MESSAGE_REPLAY,
                native_assistant_message={"role": "assistant", "content": self._native_blocks},
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
        if not self._native_blocks:
            return None
        return {"role": "assistant", "content": self._native_blocks}


# ------------------------------------------------------------------------------- helpers


def _wrap_tool(schema: dict[str, Any]) -> dict[str, Any]:
    """Anthropic names the schema field ``input_schema`` and puts it at the top level."""

    return {
        "name": schema["name"],
        "description": schema.get("description", "") or "",
        "input_schema": schema.get("parameters") or {"type": "object", "properties": {}},
    }


def _finalize_blocks(blocks: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Order the reconstructed blocks and turn accumulated tool JSON into an ``input`` object.

    A tool block whose JSON did not parse keeps its partial text under ``_partial_json`` rather than
    being dropped or given ``input: {}`` — an empty input is indistinguishable from a zero-argument
    call, and replaying it would execute something the model did not finish asking for.
    """

    import json

    out: list[dict[str, Any]] = []
    for index in sorted(blocks):
        block = dict(blocks[index])
        partial = block.pop("_partial_json", None)
        if block.get("type") == "tool_use" and isinstance(partial, str):
            try:
                parsed = json.loads(partial) if partial.strip() else {}
            except ValueError:
                block["_incomplete_input"] = partial
                out.append(block)
                continue
            block["input"] = parsed if isinstance(parsed, dict) else {}
        if block:
            out.append(block)
    return out


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
    usage = TokenUsage(
        input_tokens=_count(raw.get("input_tokens")),
        cached_input_tokens=_count(raw.get("cache_read_input_tokens")),
        output_tokens=_count(raw.get("output_tokens")),
    )
    if not any((usage.input_tokens, usage.cached_input_tokens, usage.output_tokens)):
        return None
    return NormalizedModelEvent(type=ModelEventType.USAGE, usage=usage, response_id=response_id)


def _stream_error(error: dict[str, Any]) -> ErrorType:
    kind = str(error.get("type") or "").lower()
    mapping = {
        "overloaded_error": ErrorType.PROVIDER_OVERLOADED,
        "rate_limit_error": ErrorType.PROVIDER_RATE_LIMITED,
        "authentication_error": ErrorType.AUTHENTICATION_FAILED,
        "permission_error": ErrorType.PERMISSION_DENIED,
        "not_found_error": ErrorType.MODEL_NOT_FOUND,
        "invalid_request_error": ErrorType.INVALID_REQUEST,
        "api_error": ErrorType.UNKNOWN,
    }
    if kind in mapping:
        return mapping[kind]
    code = error.get("status") or error.get("code")
    if isinstance(code, int):
        return classify_http_status(code)
    return ErrorType.UNKNOWN


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _index(event: dict[str, Any]) -> int:
    index = event.get("index")
    return index if isinstance(index, int) and not isinstance(index, bool) else 0


from dataclasses import dataclass  # noqa: E402 - used only by the subclass below


@dataclass(frozen=True)
class _TurnWithId(AssembledTurn):
    response_id: str | None = None
