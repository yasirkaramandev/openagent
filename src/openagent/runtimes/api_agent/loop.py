"""The API agent loop (spec §27).

For API models that only emit text/tool-calls, OpenAgent runs the loop itself: ask the model, run any
tool calls it requests, feed results back, repeat until it calls ``finish_task`` (or produces a
final text answer), bounded by ``agent.max_steps``. Every step emits :class:`NormalizedEvent`s so the
TUI/CLI render it exactly like a CLI-runtime run.

**Cancellation is real here** (item 9). The loop holds a :class:`RunCancellation` and checks it before
each provider request, while consuming the provider stream, before and after every tool call, and at
the top of every step. On cancellation it abandons the stream (which tears down the HTTP response),
stops running tools, and returns a ``cancelled`` outcome — it never goes on to report ``completed``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ...core.cancellation import RunCancellation, RunCancelled
from ...core.errors import ErrorType
from ...core.events import (
    EventType,
    ItemStatus,
    ModelEventType,
    NormalizedEvent,
    TokenUsage,
    ToolCall,
)
from ...core.limits import RUNTIME_LIMITS
from ...core.models import AgentProfile
from ...providers.base import Message, NormalizedModelRequest, ProviderAdapter, Role
from ...tools.control import TaskFinished
from ...tools.registry import ToolExecutor, schemas_for_profile
from .context import build_initial_messages, build_system_prompt

Emit = Callable[[NormalizedEvent], None]

_MAX_TOOL_RESULT_CHARS = 8000


@dataclass
class ApiRunOutcome:
    completed: bool
    summary: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    steps: int = 0
    error_type: str | None = None
    error_message: str | None = None
    cancelled: bool = False


async def run_api_agent(
    *,
    run_id: str,
    agent: AgentProfile,
    prompt: str,
    adapter: ProviderAdapter,
    executor: ToolExecutor,
    workspace_root: Path,
    emit: Emit,
    temperature: float | None = None,
    workspace_note: str = "",
    cancellation: RunCancellation | None = None,
) -> ApiRunOutcome:
    model = agent.runtime.model or ""
    system = build_system_prompt(agent, workspace_note)
    conversation = build_initial_messages(agent, prompt, workspace_root)
    tools = schemas_for_profile(executor.ctx.profile)
    total = TokenUsage()
    cancel = cancellation or RunCancellation(run_id)
    cancel.bind()

    def _emit(type_: EventType, source: str = "api-agent", **data: object) -> None:
        emit(NormalizedEvent(run_id=run_id, type=type_, source=source, data=dict(data)))

    def _cancelled(step: int) -> ApiRunOutcome:
        return ApiRunOutcome(
            completed=False,
            cancelled=True,
            usage=total,
            steps=step,
            error_type="user_cancelled",
            error_message=cancel.reason or "cancelled by user",
        )

    for step in range(1, agent.max_steps + 1):
        if cancel.cancelled:  # a new step must never begin after a cancel (item 9)
            return _cancelled(step - 1)

        _emit(EventType.MESSAGE_STARTED, step=step)
        history_bytes = len(
            json.dumps(
                [message.model_dump(mode="json") for message in conversation],
                ensure_ascii=False,
            ).encode("utf-8")
        )
        if (
            len(conversation) > RUNTIME_LIMITS.history_messages
            or history_bytes > RUNTIME_LIMITS.history_bytes
        ):
            return ApiRunOutcome(
                completed=False,
                usage=total,
                steps=step,
                error_type=ErrorType.OUTPUT_LIMIT_EXCEEDED.value,
                error_message="conversation history exceeded the configured byte/message limit",
            )
        request = NormalizedModelRequest(
            model=model,
            messages=conversation,
            tools=tools,
            system=system,
            temperature=temperature,
            stream=True,
        )

        text_parts: list[str] = []
        text_bytes = 0
        tool_calls: list[ToolCall] = []
        error_type = error_message = None
        stream = adapter.stream_response(request)
        iterator = stream.__aiter__()
        try:
            while True:
                # Consume the stream one event at a time through ``cancel.guard`` (item 9.1). A plain
                # ``async for`` only re-checks cancellation when a *new* chunk arrives, so a provider
                # that accepts the request and then goes silent would hang forever despite a Cancel.
                # ``guard`` races the pending ``__anext__`` against the cancellation event: the moment
                # cancellation is requested it cancels that read (tearing down the HTTP response) and
                # raises ``RunCancelled`` — even if no chunk ever comes.
                try:
                    event = await cancel.guard(iterator.__anext__())
                except StopAsyncIteration:
                    break
                if event.type == ModelEventType.TEXT_DELTA and event.text:
                    text_parts.append(event.text)
                    text_bytes += len(event.text.encode("utf-8"))
                    if text_bytes > RUNTIME_LIMITS.model_text_bytes:
                        return ApiRunOutcome(
                            completed=False,
                            usage=total,
                            steps=step,
                            error_type=ErrorType.OUTPUT_LIMIT_EXCEEDED.value,
                            error_message="model text exceeded 1 MiB",
                        )
                    _emit(EventType.MESSAGE_DELTA, text=event.text, step=step)
                elif event.type == ModelEventType.TOOL_CALL and event.tool_call is not None:
                    call = event.tool_call
                    if not call.id or not call.name:
                        error_type = ErrorType.INVALID_TOOL_CALL.value
                        error_message = "provider tool call is missing a non-empty id or name"
                        continue
                    try:
                        argument_bytes = len(
                            json.dumps(call.arguments, ensure_ascii=False).encode("utf-8")
                        )
                    except (TypeError, ValueError):
                        argument_bytes = RUNTIME_LIMITS.tool_arguments_bytes + 1
                    if argument_bytes > RUNTIME_LIMITS.tool_arguments_bytes:
                        error_type = ErrorType.INVALID_TOOL_ARGUMENTS.value
                        error_message = "provider tool arguments exceed 64 KiB or are not JSON"
                        continue
                    if len(tool_calls) >= RUNTIME_LIMITS.tool_calls_per_turn:
                        error_type = ErrorType.OUTPUT_LIMIT_EXCEEDED.value
                        error_message = "provider emitted more than 64 tool calls in one turn"
                        continue
                    tool_calls.append(event.tool_call)
                elif event.type == ModelEventType.USAGE and event.usage is not None:
                    total.input_tokens += event.usage.input_tokens
                    total.cached_input_tokens += event.usage.cached_input_tokens
                    total.output_tokens += event.usage.output_tokens
                    total.reasoning_tokens += event.usage.reasoning_tokens
                    if event.usage.provider_cost is not None:
                        total.provider_cost = (
                            total.provider_cost or 0.0
                        ) + event.usage.provider_cost
                    _emit(EventType.USAGE_UPDATED, **event.usage.model_dump())
                elif event.type == ModelEventType.ERROR:
                    error_type, error_message = event.error_type, event.error_message
        except RunCancelled:
            await _aclose(stream)
            return _cancelled(step)
        finally:
            await _aclose(stream)

        text = "".join(text_parts)
        if error_type is not None:
            return ApiRunOutcome(
                completed=False,
                usage=total,
                steps=step,
                error_type=error_type,
                error_message=error_message,
            )
        # An empty turn is a failure, not a silent success (§12). The provider stream closed having
        # produced no text AND no tool calls — a hiccup, a content filter, a truncated stream, or a
        # response whose every chunk failed to parse. Reporting `completed` with an empty summary
        # tells the user the task succeeded when the model said nothing at all. Emit no
        # `message.completed` for it either: the event log must not corroborate a success that did
        # not happen.
        #
        # NB: this must come AFTER the provider-error check below — an error stream also carries no
        # text, and reporting it as "empty" would mask the real cause (e.g. authentication_failed).
        if error_type is None and not tool_calls and not text.strip():
            _emit(
                EventType.MESSAGE_COMPLETED,
                item_id=f"msg_{step}",
                status=ItemStatus.FAILED.value,
                text="",
                tool_calls=[],
                step=step,
            )
            return ApiRunOutcome(
                completed=False,
                usage=total,
                steps=step,
                error_type="empty_model_response",
                error_message=(
                    "the model returned no text and no tool calls; the response was empty"
                ),
            )

        _emit(
            EventType.MESSAGE_COMPLETED,
            item_id=f"msg_{step}",
            status=ItemStatus.COMPLETED.value,
            text=text,
            tool_calls=[c.name for c in tool_calls],
            step=step,
        )

        conversation.append(Message(role=Role.ASSISTANT, content=text, tool_calls=tool_calls))

        if not tool_calls:  # final text answer
            return ApiRunOutcome(completed=True, summary=text, usage=total, steps=step)

        for call in tool_calls:
            if cancel.cancelled:  # never start another tool after a cancel
                return _cancelled(step)
            _emit(
                EventType.TOOL_REQUESTED, item_id=call.id, tool=call.name, arguments=call.arguments
            )
            _emit(
                EventType.TOOL_STARTED,
                item_id=call.id,
                tool=call.name,
                status=ItemStatus.IN_PROGRESS.value,
            )
            try:
                result = executor.execute(call)
            except TaskFinished as finished:
                _emit(
                    EventType.TOOL_COMPLETED,
                    item_id=call.id,
                    tool="finish_task",
                    status=ItemStatus.COMPLETED.value,
                    summary=finished.summary,
                )
                return ApiRunOutcome(
                    completed=True, summary=finished.summary, usage=total, steps=step
                )
            except RunCancelled:
                # A blocking tool (an approval or ask_user modal) was released by the cancel.
                _emit(
                    EventType.TOOL_FAILED,
                    item_id=call.id,
                    tool=call.name,
                    status=ItemStatus.CANCELLED.value,
                    error="run cancelled",
                )
                return _cancelled(step)

            # A tool may have blocked on the user (ask_user/approval) while a cancel arrived.
            if cancel.cancelled:
                return _cancelled(step)

            event_type = EventType.TOOL_COMPLETED if result.ok else EventType.TOOL_FAILED
            _emit(
                event_type,
                item_id=call.id,
                tool=call.name,
                ok=result.ok,
                status=ItemStatus.COMPLETED.value if result.ok else ItemStatus.FAILED.value,
            )
            conversation.append(
                Message(
                    role=Role.TOOL,
                    tool_call_id=call.id,
                    content=(result.content or ("ok" if result.ok else "failed"))[
                        :_MAX_TOOL_RESULT_CHARS
                    ],
                )
            )

    return ApiRunOutcome(
        completed=False,
        usage=total,
        steps=agent.max_steps,
        error_type="max_steps_exceeded",
        error_message=f"exceeded {agent.max_steps} steps",
    )


async def _aclose(stream: object) -> None:
    """Close a provider stream (idempotent): releases the HTTP response on cancel or error."""

    aclose = getattr(stream, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:  # noqa: BLE001 - teardown must never mask the real outcome
        pass
