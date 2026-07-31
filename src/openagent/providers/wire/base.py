"""Shared machinery every wire needs (spec §7, §8.1, §11).

Three things that are the same for every protocol and therefore live exactly once:

* **tool preparation** — normalizing schemas through the profile, withholding tools that cannot be
  expressed, and carrying the resulting loss of precision out to the caller instead of swallowing it;
* **turn → events** — converting a :class:`~..streaming.AssembledTurn` into normalized events, with
  one rule: an incomplete tool call becomes an *error*, never a silently dropped call and never a
  call presented as though its arguments had parsed;
* **usage parsing** — the OpenAI-shaped ``usage`` object, which four of the five wires return.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from ...core.errors import ErrorType
from ...core.events import ModelEventType, NormalizedModelEvent, TokenUsage
from ...core.events import ToolCall as EventToolCall
from ...core.limits import RUNTIME_LIMITS
from ..compat.profiles_v2 import CompatibilityProfile
from ..streaming import AssembledTurn
from ..streaming import ToolCall as AssembledToolCall
from ..tool_schema import ToolSchemaNormalizationResult, normalize_tool_schemas


@dataclass
class ToolPreparation:
    """What happened when a tool list was fitted to one endpoint.

    Kept as a value rather than logged, because every part of it is something a caller has to be
    able to surface: a withheld tool changes what the model can do, and a dropped *constraint*
    changes what the model may validly send while local validation still rejects it.
    """

    #: Tool entries in the wire's own shape, ready to place in the payload.
    wire_tools: list[dict[str, Any]] = field(default_factory=list)
    results: list[ToolSchemaNormalizationResult] = field(default_factory=list)
    #: Names of tools that could not be expressed at all and were therefore not sent.
    rejected: list[str] = field(default_factory=list)
    #: True when ``tool_choice="required"`` had to become ``"auto"``: the caller wanted a guaranteed
    #: tool call and will only get one if the model chooses to. A visible downgrade, never a silent one.
    tool_choice_downgraded: bool = False

    @property
    def narrows_validation(self) -> bool:
        return any(result.narrows_validation for result in self.results)

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        for result in self.results:
            for keyword in result.incompatible_keywords:
                out.append(
                    f"{result.tool_name}: this endpoint cannot express {keyword}; the model is told "
                    f"less than OpenAgent enforces locally"
                )
            out.extend(f"{result.tool_name}: {warning}" for warning in result.warnings)
        return out


def prepare_tools(
    tools: list[dict[str, Any]],
    profile: CompatibilityProfile,
    *,
    wrap: Any,
) -> ToolPreparation:
    """Normalize ``tools`` for ``profile`` and wrap the survivors in the wire's shape.

    ``wrap`` turns a normalized ``{"name", "description", "parameters"}`` into whatever the protocol
    expects around it — OpenAI's ``{"type": "function", "function": {...}}``, Anthropic's flat
    ``{"name", "input_schema"}``, and so on. Everything else about the process is identical, which is
    why it is here and not in five places.
    """

    prep = ToolPreparation()
    if not tools:
        return prep
    prep.results = normalize_tool_schemas(tools, profile)
    for result in prep.results:
        if not result.executable:
            prep.rejected.append(result.tool_name)
            continue
        prep.wire_tools.append(wrap(result.normalized_schema))
    return prep


def resolve_tool_choice(
    profile: CompatibilityProfile, requested: str | None, prep: ToolPreparation
) -> str | None:
    """Map a requested tool_choice onto the endpoint, recording a downgrade on ``prep``."""

    resolved = profile.normalize_tool_choice(requested)
    if requested == "required" and resolved != "required":
        prep.tool_choice_downgraded = True
    return resolved


def parse_openai_usage(raw: object) -> TokenUsage | None:
    """The conventional ``usage`` object.

    Reasoning tokens are counted separately rather than folded into output tokens: a reasoning-heavy
    turn would otherwise look like it produced far more visible text than it did. Only the *count*
    is taken; no reasoning text is ever read from here.
    """

    if not isinstance(raw, dict):
        return None
    prompt_details = raw.get("prompt_tokens_details")
    completion_details = raw.get("completion_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    completion_details = completion_details if isinstance(completion_details, dict) else {}
    return TokenUsage(
        input_tokens=_count(raw.get("prompt_tokens") or raw.get("input_tokens")),
        cached_input_tokens=_count(prompt_details.get("cached_tokens")),
        output_tokens=_count(raw.get("completion_tokens") or raw.get("output_tokens")),
        reasoning_tokens=_count(
            completion_details.get("reasoning_tokens") or raw.get("reasoning_tokens")
        ),
    )


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def tool_call_events(
    turn: AssembledTurn, *, response_id: str | None
) -> Iterator[NormalizedModelEvent]:
    """Emit one event per assembled tool call.

    A complete call becomes a ``tool_call``. An incomplete one becomes an ``error`` naming the tool,
    because the two ways of "handling" it that require no code are both wrong: dropping it makes the
    model appear to have said nothing, and emitting it with ``arguments={}`` makes a truncated call
    indistinguishable from a zero-argument one and executes it.
    """

    # §7.1/§7.2 — before any individual call is judged, the *turn* has to have earned the right to
    # run tools at all. A stream that stopped without a terminal event, or that contradicted itself
    # about which call is which, yields no tool calls however well-formed the fragments look. This
    # is deliberately not a per-call filter: identity contradictions are about the mapping between
    # fragments and calls, so the call that did not collide is no more trustworthy than the one that
    # did.
    if not turn.is_terminal:
        # The wire reports the interruption itself, with the transport-level detail it has and this
        # function does not. Emitting a second error here would double-report one failure.
        return
    if turn.protocol_violations:
        yield NormalizedModelEvent(
            type=ModelEventType.ERROR,
            error_type=ErrorType.PROTOCOL_MISMATCH.value,
            error_message=(f"no tool call from this turn was executed: {turn.execution_refusal}"),
            response_id=response_id,
        )
        return

    for call in turn.tool_calls:
        if not call.complete:
            yield NormalizedModelEvent(
                type=ModelEventType.ERROR,
                error_type=_incomplete_error(call).value,
                error_message=(
                    f"tool call {call.name or '<unnamed>'} was not usable: "
                    f"{call.error or 'incomplete'}"
                ),
                response_id=response_id,
            )
            continue
        if call.id is None or not call.name:
            yield NormalizedModelEvent(
                type=ModelEventType.ERROR,
                error_type=ErrorType.INVALID_TOOL_CALL.value,
                error_message="provider tool call is missing a non-empty id or name",
                response_id=response_id,
            )
            continue
        oversized = _argument_bytes(call.arguments) > RUNTIME_LIMITS.tool_arguments_bytes
        if oversized:
            yield NormalizedModelEvent(
                type=ModelEventType.ERROR,
                error_type=ErrorType.INVALID_TOOL_ARGUMENTS.value,
                error_message=f"tool call {call.name} arguments exceed the size limit",
                response_id=response_id,
            )
            continue
        yield NormalizedModelEvent(
            type=ModelEventType.TOOL_CALL,
            tool_call=EventToolCall(id=call.id, name=call.name, arguments=call.arguments),
            response_id=response_id,
        )


def _incomplete_error(call: AssembledToolCall) -> ErrorType:
    reason = (call.error or "").lower()
    if "no name" in reason:
        return ErrorType.INVALID_TOOL_CALL
    return ErrorType.INVALID_TOOL_ARGUMENTS


def _argument_bytes(arguments: dict[str, Any]) -> int:
    import json

    try:
        return len(json.dumps(arguments, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return RUNTIME_LIMITS.tool_arguments_bytes + 1


def interruption_event(turn: AssembledTurn, *, response_id: str | None) -> NormalizedModelEvent:
    """The single error reported for a turn the provider never finished.

    It names the tool calls it is discarding. The interruption is the accurate root cause — a call
    whose arguments are half-written is a *symptom* of the stream stopping — but reporting only the
    cause would leave the user unable to see what was nearly run, so both go in one event rather
    than in two that would have to be correlated.
    """

    named = [call.name for call in turn.tool_calls if call.name]
    detail = ""
    if turn.tool_calls:
        listed = ", ".join(named) if named else "unnamed"
        detail = f"; {len(turn.tool_calls)} unexecuted tool call(s) were discarded ({listed})"
    return NormalizedModelEvent(
        type=ModelEventType.ERROR,
        error_type=ErrorType.STREAM_INTERRUPTED.value,
        error_message=(f"the provider stopped streaming without a terminal event{detail}"),
        response_id=response_id,
    )


def interrupted(turn: AssembledTurn) -> bool:
    """Whether a turn ended without the provider saying why.

    Distinct from a transport failure: nothing broke at the network layer, the provider simply
    stopped. The remedy is to treat the turn as unfinished, not to diagnose connectivity.
    """

    return not turn.is_terminal
