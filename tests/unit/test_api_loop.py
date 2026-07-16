"""End-to-end test of the API agent loop with a fake adapter (no network)."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from openagent.core.cancellation import RunCancellation
from openagent.core.events import (
    ModelEventType,
    NormalizedEvent,
    NormalizedModelEvent,
    TokenUsage,
    ToolCall,
)
from openagent.core.models import AgentProfile, AgentRuntime, RuntimeType
from openagent.core.permissions import SAFE_EDIT, get_profile
from openagent.providers.base import NormalizedModelRequest
from openagent.runtimes.api_agent.loop import run_api_agent
from openagent.security.approvals import ApprovalGate
from openagent.tools.base import ToolContext
from openagent.tools.registry import ToolExecutor


class ScriptedAdapter:
    """Yields a pre-scripted sequence of turns."""

    def __init__(self, turns: list[list[NormalizedModelEvent]]) -> None:
        self.turns = turns
        self.calls = 0

    async def stream_response(
        self, request: NormalizedModelRequest
    ) -> AsyncIterator[NormalizedModelEvent]:
        turn = self.turns[self.calls]
        self.calls += 1
        for event in turn:
            yield event


def _agent() -> AgentProfile:
    return AgentProfile(
        name="tester",
        runtime=AgentRuntime(type=RuntimeType.API_AGENT, provider="p", model="m"),
        permission_profile=SAFE_EDIT,
    )


def _executor(root: Path) -> ToolExecutor:
    ctx = ToolContext(
        workspace_root=root,
        profile=get_profile(SAFE_EDIT),
        approval_gate=ApprovalGate(auto_approve=True),
        run_id="run_test",
    )
    return ToolExecutor(ctx)


async def test_loop_runs_tool_then_finishes(tmp_path: Path):
    (tmp_path / "main.py").write_text("value = 1\n")
    adapter = ScriptedAdapter(
        [
            # turn 1: request an apply_patch tool call
            [
                NormalizedModelEvent(
                    type=ModelEventType.TOOL_CALL,
                    tool_call=ToolCall(
                        id="c1",
                        name="apply_patch",
                        arguments={
                            "path": "main.py",
                            "old_string": "value = 1",
                            "new_string": "value = 2",
                        },
                    ),
                ),
                NormalizedModelEvent(
                    type=ModelEventType.USAGE, usage=TokenUsage(input_tokens=10, output_tokens=5)
                ),
                NormalizedModelEvent(type=ModelEventType.DONE),
            ],
            # turn 2: final text answer, no tool calls
            [
                NormalizedModelEvent(type=ModelEventType.TEXT_DELTA, text="Updated value to 2."),
                NormalizedModelEvent(
                    type=ModelEventType.USAGE, usage=TokenUsage(input_tokens=12, output_tokens=6)
                ),
                NormalizedModelEvent(type=ModelEventType.DONE),
            ],
        ]
    )
    events: list[NormalizedEvent] = []
    outcome = await run_api_agent(
        run_id="run_test",
        agent=_agent(),
        prompt="bump value to 2",
        adapter=adapter,
        executor=_executor(tmp_path),
        workspace_root=tmp_path,
        emit=events.append,
    )
    assert outcome.completed
    assert "value to 2" in outcome.summary
    assert (tmp_path / "main.py").read_text() == "value = 2\n"
    assert outcome.usage.output_tokens == 11  # 5 + 6 accumulated
    types = {e.type for e in events}
    assert "tool.requested" in types
    assert "tool.completed" in types
    assert "message.completed" in types


async def test_loop_accumulates_provider_cost_across_turns(tmp_path: Path):
    """provider_cost is summed across turns into the outcome usage (item 12)."""
    adapter = ScriptedAdapter(
        [
            [
                NormalizedModelEvent(
                    type=ModelEventType.USAGE,
                    usage=TokenUsage(input_tokens=10, output_tokens=5, provider_cost=0.01),
                ),
                NormalizedModelEvent(
                    type=ModelEventType.TOOL_CALL,
                    tool_call=ToolCall(id="c1", name="finish_task", arguments={"summary": "done"}),
                ),
                NormalizedModelEvent(type=ModelEventType.DONE),
            ],
        ]
    )
    events: list[NormalizedEvent] = []
    outcome = await run_api_agent(
        run_id="r",
        agent=_agent(),
        prompt="x",
        adapter=adapter,
        executor=_executor(tmp_path),
        workspace_root=tmp_path,
        emit=events.append,
    )
    assert outcome.completed
    assert outcome.usage.provider_cost == 0.01
    usage_ev = next(e for e in events if e.type == "usage.updated")
    assert usage_ev.data["provider_cost"] == 0.01
    assert "cost_usd" not in usage_ev.data


async def test_loop_finish_task_tool(tmp_path: Path):
    adapter = ScriptedAdapter(
        [
            [
                NormalizedModelEvent(
                    type=ModelEventType.TOOL_CALL,
                    tool_call=ToolCall(
                        id="c1", name="finish_task", arguments={"summary": "nothing to do"}
                    ),
                ),
                NormalizedModelEvent(type=ModelEventType.DONE),
            ],
        ]
    )
    outcome = await run_api_agent(
        run_id="r",
        agent=_agent(),
        prompt="noop",
        adapter=adapter,
        executor=_executor(tmp_path),
        workspace_root=tmp_path,
        emit=lambda e: None,
    )
    assert outcome.completed
    assert outcome.summary == "nothing to do"


async def test_loop_reports_error(tmp_path: Path):
    adapter = ScriptedAdapter(
        [
            [
                NormalizedModelEvent(
                    type=ModelEventType.ERROR,
                    error_type="authentication_failed",
                    error_message="bad key",
                )
            ],
        ]
    )
    outcome = await run_api_agent(
        run_id="r",
        agent=_agent(),
        prompt="x",
        adapter=adapter,
        executor=_executor(tmp_path),
        workspace_root=tmp_path,
        emit=lambda e: None,
    )
    assert not outcome.completed
    assert outcome.error_type == "authentication_failed"


async def test_provider_error_precedes_message_completion(tmp_path: Path):
    events: list[NormalizedEvent] = []
    adapter = ScriptedAdapter(
        [
            [
                NormalizedModelEvent(type=ModelEventType.TEXT_DELTA, text="partial"),
                NormalizedModelEvent(
                    type=ModelEventType.ERROR,
                    error_type="provider_overloaded",
                    error_message="failed after partial output",
                ),
            ]
        ]
    )
    outcome = await run_api_agent(
        run_id="r",
        agent=_agent(),
        prompt="x",
        adapter=adapter,
        executor=_executor(tmp_path),
        workspace_root=tmp_path,
        emit=events.append,
    )
    assert outcome.error_type == "provider_overloaded"
    assert not any(event.type == "message.completed" for event in events)


async def test_missing_tool_identity_is_a_distinct_failure(tmp_path: Path):
    adapter = ScriptedAdapter(
        [
            [
                NormalizedModelEvent(
                    type=ModelEventType.TOOL_CALL,
                    tool_call=ToolCall(id="", name="", arguments={}),
                )
            ]
        ]
    )
    outcome = await run_api_agent(
        run_id="r",
        agent=_agent(),
        prompt="x",
        adapter=adapter,
        executor=_executor(tmp_path),
        workspace_root=tmp_path,
        emit=lambda _event: None,
    )
    assert outcome.error_type == "invalid_tool_call"


async def test_model_text_limit_is_visible(tmp_path: Path):
    adapter = ScriptedAdapter(
        [[NormalizedModelEvent(type=ModelEventType.TEXT_DELTA, text="x" * (1024 * 1024 + 1))]]
    )
    outcome = await run_api_agent(
        run_id="r",
        agent=_agent(),
        prompt="x",
        adapter=adapter,
        executor=_executor(tmp_path),
        workspace_root=tmp_path,
        emit=lambda _event: None,
    )
    assert outcome.error_type == "output_limit_exceeded"


# --------------------------------------------------------------------------- stalled-stream cancel (9.1)


class StallingAdapter:
    """Accepts the request, then never yields another event — a hung/silent provider.

    The whole point of item 9.1: with a plain ``async for`` the loop only re-checks cancellation when
    a *new* chunk arrives, so a provider that goes quiet after the request pins the run forever. The
    stream also records whether it was ``aclose()``d, so the test can prove the HTTP response is torn
    down rather than left dangling.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.aclosed = False

    def stream_response(self, request: NormalizedModelRequest) -> "StallingAdapter._Stream":
        return self._Stream(self)

    class _Stream:
        def __init__(self, parent: "StallingAdapter") -> None:
            self.parent = parent

        def __aiter__(self) -> "StallingAdapter._Stream":
            return self

        async def __anext__(self) -> NormalizedModelEvent:
            self.parent.entered.set()
            await asyncio.Event().wait()  # block forever: the provider produced nothing more
            raise StopAsyncIteration  # pragma: no cover - unreachable

        async def aclose(self) -> None:
            self.parent.aclosed = True


async def test_stalled_stream_is_cancelled_without_a_new_chunk(tmp_path: Path):
    """Cancelling a run whose provider is mid-stream but silent really stops it (item 9.1)."""

    adapter = StallingAdapter()
    cancel = RunCancellation("run_stall")
    events: list[NormalizedEvent] = []
    task = asyncio.create_task(
        run_api_agent(
            run_id="run_stall",
            agent=_agent(),
            prompt="x",
            adapter=adapter,
            executor=_executor(tmp_path),
            workspace_root=tmp_path,
            emit=events.append,
            cancellation=cancel,
        )
    )

    await asyncio.wait_for(adapter.entered.wait(), timeout=2)  # the loop is now awaiting __anext__
    cancel.cancel("user requested stop")
    outcome = await asyncio.wait_for(task, timeout=5)  # no hang: the guarded read is abandoned

    assert outcome.cancelled and not outcome.completed
    assert outcome.error_type == "user_cancelled"
    assert adapter.aclosed, "the stalled stream must be aclose()d on cancel"
    assert not any(e.type == "run.completed" for e in events)
