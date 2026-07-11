"""End-to-end test of the API agent loop with a fake adapter (no network)."""

from collections.abc import AsyncIterator
from pathlib import Path

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
        workspace_root=root, profile=get_profile(SAFE_EDIT),
        approval_gate=ApprovalGate(auto_approve=True), run_id="run_test",
    )
    return ToolExecutor(ctx)


async def test_loop_runs_tool_then_finishes(tmp_path: Path):
    (tmp_path / "main.py").write_text("value = 1\n")
    adapter = ScriptedAdapter([
        # turn 1: request an apply_patch tool call
        [
            NormalizedModelEvent(
                type=ModelEventType.TOOL_CALL,
                tool_call=ToolCall(id="c1", name="apply_patch", arguments={
                    "path": "main.py", "old_string": "value = 1", "new_string": "value = 2"}),
            ),
            NormalizedModelEvent(type=ModelEventType.USAGE, usage=TokenUsage(input_tokens=10, output_tokens=5)),
            NormalizedModelEvent(type=ModelEventType.DONE),
        ],
        # turn 2: final text answer, no tool calls
        [
            NormalizedModelEvent(type=ModelEventType.TEXT_DELTA, text="Updated value to 2."),
            NormalizedModelEvent(type=ModelEventType.USAGE, usage=TokenUsage(input_tokens=12, output_tokens=6)),
            NormalizedModelEvent(type=ModelEventType.DONE),
        ],
    ])
    events: list[NormalizedEvent] = []
    outcome = await run_api_agent(
        run_id="run_test", agent=_agent(), prompt="bump value to 2",
        adapter=adapter, executor=_executor(tmp_path), workspace_root=tmp_path,
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


async def test_loop_finish_task_tool(tmp_path: Path):
    adapter = ScriptedAdapter([
        [
            NormalizedModelEvent(
                type=ModelEventType.TOOL_CALL,
                tool_call=ToolCall(id="c1", name="finish_task", arguments={"summary": "nothing to do"}),
            ),
            NormalizedModelEvent(type=ModelEventType.DONE),
        ],
    ])
    outcome = await run_api_agent(
        run_id="r", agent=_agent(), prompt="noop", adapter=adapter,
        executor=_executor(tmp_path), workspace_root=tmp_path, emit=lambda e: None,
    )
    assert outcome.completed
    assert outcome.summary == "nothing to do"


async def test_loop_reports_error(tmp_path: Path):
    adapter = ScriptedAdapter([
        [NormalizedModelEvent(type=ModelEventType.ERROR, error_type="authentication_failed",
                              error_message="bad key")],
    ])
    outcome = await run_api_agent(
        run_id="r", agent=_agent(), prompt="x", adapter=adapter,
        executor=_executor(tmp_path), workspace_root=tmp_path, emit=lambda e: None,
    )
    assert not outcome.completed
    assert outcome.error_type == "authentication_failed"
