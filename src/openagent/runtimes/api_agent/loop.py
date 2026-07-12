"""The API agent loop (spec §27).

For API models that only emit text/tool-calls, OpenAgent runs the loop itself: ask the model, run any
tool calls it requests, feed results back, repeat until it calls ``finish_task`` (or produces a
final text answer), bounded by ``agent.max_steps``. Every step emits :class:`NormalizedEvent`s so the
TUI/CLI render it exactly like a CLI-runtime run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ...core.events import (
    EventType,
    ModelEventType,
    NormalizedEvent,
    TokenUsage,
)
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
) -> ApiRunOutcome:
    model = agent.runtime.model or ""
    system = build_system_prompt(agent, workspace_note)
    conversation = build_initial_messages(agent, prompt, workspace_root)
    tools = schemas_for_profile(executor.ctx.profile)
    total = TokenUsage()

    def _emit(type_: EventType, source: str = "api-agent", **data: object) -> None:
        emit(NormalizedEvent(run_id=run_id, type=type_, source=source, data=dict(data)))

    for step in range(1, agent.max_steps + 1):
        _emit(EventType.MESSAGE_STARTED, step=step)
        request = NormalizedModelRequest(
            model=model, messages=conversation, tools=tools, system=system,
            temperature=temperature, stream=True,
        )

        text_parts: list[str] = []
        tool_calls = []
        error_type = error_message = None
        async for event in adapter.stream_response(request):
            if event.type == ModelEventType.TEXT_DELTA and event.text:
                text_parts.append(event.text)
                _emit(EventType.MESSAGE_DELTA, text=event.text)
            elif event.type == ModelEventType.TOOL_CALL and event.tool_call is not None:
                tool_calls.append(event.tool_call)
            elif event.type == ModelEventType.USAGE and event.usage is not None:
                total.input_tokens += event.usage.input_tokens
                total.cached_input_tokens += event.usage.cached_input_tokens
                total.output_tokens += event.usage.output_tokens
                if event.usage.provider_cost is not None:
                    total.provider_cost = (total.provider_cost or 0.0) + event.usage.provider_cost
                _emit(EventType.USAGE_UPDATED, **event.usage.model_dump())
            elif event.type == ModelEventType.ERROR:
                error_type, error_message = event.error_type, event.error_message

        text = "".join(text_parts)
        _emit(EventType.MESSAGE_COMPLETED, text=text, tool_calls=[c.name for c in tool_calls])

        if error_type is not None:
            return ApiRunOutcome(
                completed=False, usage=total, steps=step,
                error_type=error_type, error_message=error_message,
            )

        conversation.append(Message(role=Role.ASSISTANT, content=text, tool_calls=tool_calls))

        if not tool_calls:  # final text answer
            return ApiRunOutcome(completed=True, summary=text, usage=total, steps=step)

        for call in tool_calls:
            _emit(EventType.TOOL_REQUESTED, tool=call.name, arguments=call.arguments)
            _emit(EventType.TOOL_STARTED, tool=call.name)
            try:
                result = executor.execute(call)
            except TaskFinished as finished:
                _emit(EventType.TOOL_COMPLETED, tool="finish_task", summary=finished.summary)
                return ApiRunOutcome(
                    completed=True, summary=finished.summary, usage=total, steps=step
                )
            event_type = EventType.TOOL_COMPLETED if result.ok else EventType.TOOL_FAILED
            _emit(event_type, tool=call.name, ok=result.ok)
            conversation.append(
                Message(
                    role=Role.TOOL,
                    tool_call_id=call.id,
                    content=(result.content or ("ok" if result.ok else "failed"))[:_MAX_TOOL_RESULT_CHARS],
                )
            )

    return ApiRunOutcome(
        completed=False, usage=total, steps=agent.max_steps,
        error_type="unknown", error_message=f"exceeded {agent.max_steps} steps",
    )
