"""The Ollama native chat wire (spec §12.2).

Ollama's OpenAI-compatible endpoint exists, and using it would cost two things v0.2 needs:
``thinking`` as a field separate from ``content``, and the native assistant message that a tool
continuation has to replay. So the native ``/api/chat`` is the primary transport.

Three things about it differ from every other wire here and each one is a place a naive port breaks:

* **NDJSON, not SSE.** One bare JSON object per line. An SSE reader drops every line and reports a
  successful empty turn.
* **Tool calls have no ids.** Ollama returns ``{"function": {"name", "arguments"}}`` and matches tool
  *results* by ``tool_name``. OpenAgent's normalized :class:`~openagent.core.events.ToolCall` requires
  an id, so one is synthesized — and it encodes the tool name, because that is the only handle the
  provider will accept back.
* **``thinking`` is a first-class field.** Kept out of ``content`` on the way out and preserved on the
  way back in, since a model that reasoned and had its reasoning stripped answers the next turn worse.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ...core.errors import ErrorType
from ...core.events import ModelEventType, NormalizedModelEvent, TokenUsage
from ...core.models import Protocol
from ..base import Message, NormalizedModelRequest, Role
from ..compat.profiles_v2 import CompatibilityProfile
from ..continuation import ContinuationEnvelope, ContinuationStrategy
from ..error_mapping import ProviderErrorSignal, map_provider_error
from ..streaming import AssembledTurn, StreamingTurnAssembler, Usage
from ..transport import Transport, TransportError
from .base import ToolPreparation, interrupted, prepare_tools, tool_call_events

_PATH = "/api/chat"

#: Prefix for synthesized tool-call ids. Ollama sends no id and keys results by name, so the id has
#: to be reversible: ``ollama:<index>:<name>`` round-trips to the name the provider expects back.
_ID_PREFIX = "ollama"

_THINK_OFF = {"off", "none", "disabled"}
#: Ollama accepts ``think: true`` and, on newer builds, a level string. Levels are passed through
#: only when they are ones Ollama documents; anything else becomes a plain ``true`` rather than a
#: value the server may reject.
_THINK_LEVELS = {"low", "medium", "high"}


def tool_name_from_id(call_id: str) -> str | None:
    """Recover the tool name from a synthesized id, or ``None`` if it is not one of ours."""

    if not call_id.startswith(f"{_ID_PREFIX}:"):
        return None
    parts = call_id.split(":", 2)
    return parts[2] if len(parts) == 3 and parts[2] else None


def synthesize_tool_id(index: int, name: str) -> str:
    return f"{_ID_PREFIX}:{index}:{name}"


class OllamaNativeWire:
    """Serialize to, and read from, Ollama's native chat endpoint."""

    protocol = Protocol.OLLAMA_NATIVE_CHAT

    def __init__(
        self,
        *,
        profile: CompatibilityProfile,
        transport: Transport,
        reasoning_effort: str | None = None,
        keep_alive: str | None = None,
    ) -> None:
        self.profile = profile
        self.transport = transport
        self.reasoning_effort = reasoning_effort
        #: Passed through verbatim when set. Never defaulted: choosing how long a model stays resident
        #: in someone else's RAM is not OpenAgent's decision to make silently (spec §12.4).
        self.keep_alive = keep_alive
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
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": self._messages(request),
            "stream": bool(stream),
        }

        options: dict[str, Any] = {}
        if request.max_tokens:
            options["num_predict"] = request.max_tokens
        temperature = self.profile.clamp_temperature(request.temperature)
        if temperature is not None:
            options["temperature"] = temperature
        if options:
            payload["options"] = options

        prep = prepare_tools(request.tools, self.profile, wrap=_wrap_tool)
        if prep.wire_tools:
            payload["tools"] = prep.wire_tools
            # Ollama documents no tool_choice parameter. A caller asking for a guaranteed tool call
            # cannot get one here, and saying so is better than sending a field that is ignored.
            if tool_choice == "required":
                prep.tool_choice_downgraded = True

        think = self._think_field()
        if think is not None:
            payload["think"] = think
        if self.keep_alive is not None:
            payload["keep_alive"] = self.keep_alive

        for key, value in self.profile.extra_request_fields.items():
            payload.setdefault(key, value)
        for param in self.profile.drop_params:
            payload.pop(param, None)
        return payload, prep

    def _think_field(self) -> bool | str | None:
        effort = self.reasoning_effort
        if effort is None:
            return None
        lowered = effort.lower()
        if lowered in _THINK_OFF:
            return False
        return lowered if lowered in _THINK_LEVELS else True

    def _messages(self, request: NormalizedModelRequest) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for message in request.messages:
            messages.append(self._serialize(message))
        return messages

    def _serialize(self, message: Message) -> dict[str, Any]:
        role = message.role.value if hasattr(message.role, "value") else str(message.role)

        if role == Role.TOOL.value:
            # Ollama matches a result to its call by tool *name*. The id we synthesized carries it;
            # ``name`` is the fallback for a history built elsewhere.
            name = tool_name_from_id(message.tool_call_id or "") or message.name or None
            if not name:
                raise ValueError(
                    "Ollama matches a tool result to its call by tool name; this result has neither "
                    "a recoverable call id nor a name"
                )
            return {"role": "tool", "content": message.content or "", "tool_name": name}

        if role == Role.ASSISTANT.value:
            if message.raw_blocks:
                first = next((b for b in message.raw_blocks if isinstance(b, dict)), None)
                if first is not None:
                    return dict(first)
            entry: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {"function": {"name": call.name, "arguments": call.arguments}}
                    for call in message.tool_calls
                ]
            return entry

        return {"role": role, "content": message.content}

    # ------------------------------------------------------------------ execution

    async def events(
        self, request: NormalizedModelRequest, *, tool_choice: str | None = "auto"
    ) -> AsyncIterator[NormalizedModelEvent]:
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
        message = _dict(data.get("message"))
        assembler = StreamingTurnAssembler()

        text = message.get("content")
        if isinstance(text, str) and text:
            assembler.append_text(text)
            yield NormalizedModelEvent(type=ModelEventType.TEXT_DELTA, text=text)

        thinking = message.get("thinking")
        if isinstance(thinking, str):
            assembler.append_reasoning(thinking)

        self._register_calls(assembler, message.get("tool_calls"))
        assembler.set_usage(_usage(data))
        assembler.set_finish_reason(_finish(data))

        turn = assembler.build()
        self.last_turn = turn
        self._native_assistant_message = dict(message) if message else None

        for event in tool_call_events(turn, response_id=None):
            yield event
        usage_event = _usage_event(data)
        if usage_event is not None:
            yield usage_event
        yield NormalizedModelEvent(type=ModelEventType.DONE)

    async def _stream(self, payload: dict[str, Any]) -> AsyncIterator[NormalizedModelEvent]:
        assembler = StreamingTurnAssembler()
        saw_done = False
        final: dict[str, Any] = {}
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls_seen: list[dict[str, Any]] = []

        async for chunk in self.transport.stream_ndjson(_PATH, payload):
            message = _dict(chunk.get("message"))

            content = message.get("content")
            if isinstance(content, str) and content:
                assembler.append_text(content)
                text_parts.append(content)
                yield NormalizedModelEvent(type=ModelEventType.TEXT_DELTA, text=content)

            thinking = message.get("thinking")
            if isinstance(thinking, str) and thinking:
                assembler.append_reasoning(thinking)
                thinking_parts.append(thinking)

            calls = message.get("tool_calls")
            if isinstance(calls, list) and calls:
                # Ollama emits complete call objects rather than argument fragments, so each one is
                # registered whole. The assembler is still the single place a call becomes final.
                self._register_calls(assembler, calls, offset=len(tool_calls_seen))
                tool_calls_seen.extend(c for c in calls if isinstance(c, dict))

            if chunk.get("done") is True:
                saw_done = True
                final = chunk
                assembler.set_usage(_usage(chunk))
                assembler.set_finish_reason(_finish(chunk))

        if not saw_done:
            assembler.mark_interrupted()

        turn = assembler.build()
        self.last_turn = turn
        native: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
        if thinking_parts:
            native["thinking"] = "".join(thinking_parts)
        if tool_calls_seen:
            native["tool_calls"] = tool_calls_seen
        self._native_assistant_message = native

        for event in tool_call_events(turn, response_id=None):
            yield event
        usage_event = _usage_event(final)
        if usage_event is not None:
            yield usage_event
        if interrupted(turn) and not turn.tool_calls:
            yield NormalizedModelEvent(
                type=ModelEventType.ERROR,
                error_type=ErrorType.STREAM_INTERRUPTED.value,
                error_message="the local server stopped streaming without a done marker",
            )
            return
        yield NormalizedModelEvent(type=ModelEventType.DONE)

    def _register_calls(
        self, assembler: StreamingTurnAssembler, calls: object, *, offset: int = 0
    ) -> None:
        for position, call in enumerate(calls if isinstance(calls, list) else []):
            if not isinstance(call, dict):
                continue
            function = _dict(call.get("function"))
            name = function.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            index = offset + position
            assembler.register_tool_call(
                index=index, tool_id=synthesize_tool_id(index, name), name=name
            )
            arguments = function.get("arguments")
            if isinstance(arguments, dict):
                assembler.append_tool_argument_fragment(json.dumps(arguments), index=index)
            elif isinstance(arguments, str):
                assembler.append_tool_argument_fragment(arguments, index=index)

    # ------------------------------------------------------------------ continuation

    def build_continuation(self, *, model_id: str | None = None) -> ContinuationEnvelope:
        """Native replay whenever the turn reasoned or called a tool.

        A plain text turn is fully described by normalized history, so no artifact is stored for it.
        Anything with ``thinking`` or ``tool_calls`` is not: the thinking field and the exact
        ``function`` objects are what the next request has to carry.
        """

        native = self._native_assistant_message or {}
        if native.get("thinking") or native.get("tool_calls"):
            return ContinuationEnvelope.build(
                provider_type=self.profile.provider_type,
                protocol=self.protocol,
                strategy=ContinuationStrategy.NATIVE_MESSAGE_REPLAY,
                native_assistant_message=native,
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


def _wrap_tool(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": schema["name"],
            "description": schema.get("description", "") or "",
            "parameters": schema.get("parameters") or {"type": "object", "properties": {}},
        },
    }


def _usage(data: dict[str, Any]) -> Usage | None:
    prompt = _count(data.get("prompt_eval_count"))
    output = _count(data.get("eval_count"))
    if not prompt and not output:
        return None
    return Usage(input_tokens=prompt, output_tokens=output)


def _usage_event(data: dict[str, Any]) -> NormalizedModelEvent | None:
    if not data:
        return None
    prompt = _count(data.get("prompt_eval_count"))
    output = _count(data.get("eval_count"))
    if not prompt and not output:
        return None
    return NormalizedModelEvent(
        type=ModelEventType.USAGE,
        usage=TokenUsage(input_tokens=prompt, output_tokens=output),
    )


def _finish(data: dict[str, Any]) -> str | None:
    reason = data.get("done_reason")
    if not isinstance(reason, str) or not reason:
        return "stop" if data.get("done") is True else None
    # Ollama's own vocabulary. "load" means the model was (re)loaded and nothing was generated —
    # not a completed turn, which is what mapping it to "stop" would claim.
    return {"stop": "stop", "length": "length", "load": "interrupted"}.get(reason, reason)


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _dict(value: object) -> dict[str, Any]:
    """``value`` if it is a mapping, else an empty one.

    A named helper rather than an inline ``x if isinstance(x, dict) else {}``: the inline form looks up
    the key twice and, because the isinstance check applies to a *different* call expression, narrows
    nothing — so every downstream ``.get`` is untyped. One helper fixes both.
    """

    return value if isinstance(value, dict) else {}
