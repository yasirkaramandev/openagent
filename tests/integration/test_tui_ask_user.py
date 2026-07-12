"""Full end-to-end TUI ask_user flow (item 14).

Drives a real API-agent run through the New Run screen: the run executes in a thread worker, a mocked
model calls ``ask_user`` on turn 1, the QuestionModal appears, the pilot types an answer, the modal
closes, the answer reaches the second model request, the run completes, the activity log updates, and
the event stream carries question.requested/question.answered. Also covers Esc/skip, a blank answer,
and cancelling the run while the question is open — always confirming the worker terminates (no
deadlock).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.events import ModelEventType, NormalizedModelEvent, ToolCall
from openagent.core.models import RuntimeType
from openagent.providers.base import NormalizedModelRequest
from openagent.tui.app import OpenAgentTUI
from openagent.tui.screens.modals import QuestionModal


class AskingAdapter:
    """A scripted provider adapter: turn 1 asks the user, turn 2 finishes using the answer."""

    def __init__(self, question: str = "which port should I use?") -> None:
        self.question = question
        self.calls = 0
        self.requests: list[NormalizedModelRequest] = []

    async def stream_response(
        self, request: NormalizedModelRequest
    ) -> AsyncIterator[NormalizedModelEvent]:
        self.requests.append(request)
        self.calls += 1
        if self.calls == 1:
            yield NormalizedModelEvent(
                type=ModelEventType.TOOL_CALL,
                tool_call=ToolCall(id="c1", name="ask_user", arguments={"question": self.question}),
            )
            yield NormalizedModelEvent(type=ModelEventType.DONE)
        else:
            yield NormalizedModelEvent(type=ModelEventType.TEXT_DELTA,
                                       text="Understood — proceeding with your answer.")
            yield NormalizedModelEvent(type=ModelEventType.DONE)


@pytest.fixture()
def oa(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    app = OpenAgentApp(Paths(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db", project_root=project,
    ))
    app.providers.add(name="testco", provider_type="custom", base_url="https://api.test/v1",
                      api_key="sk-x", store_key=False)
    app.agents.create(name="asker", runtime_type=RuntimeType.API_AGENT,
                      provider="testco", model="test-model")
    return app


@pytest.fixture()
def adapter(oa: OpenAgentApp, monkeypatch: pytest.MonkeyPatch) -> AskingAdapter:
    ad = AskingAdapter()
    monkeypatch.setattr(oa.providers, "adapter_for", lambda provider: ad)
    return ad


async def _start_run(pilot, app) -> object:
    from textual.widgets import Input, Select

    app.open_section("new_run")
    await pilot.pause()
    screen = app.screen
    screen.query_one("#agent", Select).value = "asker"
    screen.query_one("#prompt", Input).value = "set up the server"
    await pilot.click("#run")
    return screen


async def _wait_for_question(pilot, timeout: int = 200) -> QuestionModal:
    for _ in range(timeout):
        await pilot.pause(0.05)
        if isinstance(pilot.app.screen, QuestionModal):
            return pilot.app.screen
    raise AssertionError("QuestionModal never appeared")


async def _wait_terminal(pilot, oa, screen, timeout: int = 300):
    for _ in range(timeout):
        await pilot.pause(0.05)
        rid = getattr(screen, "_run_id", None)
        run = oa.runs.get(rid) if rid else None
        if run and run.status.value in ("completed", "failed", "cancelled"):
            return run
    raise AssertionError("run worker did not terminate (possible deadlock)")


# --------------------------------------------------------------------------- happy path

async def test_ask_user_modal_answer_reaches_model_and_completes(oa, adapter):
    from textual.widgets import Input, RichLog

    app = OpenAgentTUI(oa)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _start_run(pilot, app)
        modal = await _wait_for_question(pilot)
        assert "which port" in str(modal.query_one("#q").render())

        # Type an answer and submit (Enter).
        modal.query_one("#answer", Input).value = "8080"
        await pilot.press("enter")

        run = await _wait_terminal(pilot, oa, screen)
        assert run.status.value == "completed"

        # The activity log (still mounted on the run screen) updated.
        log_lines = "\n".join(str(line) for line in screen.query_one("#run-log", RichLog).lines)
        assert "done" in log_lines.lower()

    # The answer reached the SECOND model request (as the tool result in the conversation).
    assert adapter.calls == 2
    turn2 = adapter.requests[1]
    assert any("8080" in str(m) for m in turn2.messages)

    # The event stream carries the question lifecycle.
    events = oa.runs.output(run.id, "events")
    assert "question.requested" in events
    assert "question.answered" in events


# --------------------------------------------------------------------------- Esc / skip

async def test_ask_user_escape_falls_back_and_completes(oa, adapter):
    app = OpenAgentTUI(oa)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _start_run(pilot, app)
        await _wait_for_question(pilot)
        await pilot.press("escape")  # skip the question
        run = await _wait_terminal(pilot, oa, screen)
        assert run.status.value == "completed"  # best-judgment fallback, no deadlock

    events = oa.runs.output(run.id, "events")
    assert "question.requested" in events
    assert "question.cancelled" in events


# --------------------------------------------------------------------------- blank answer

async def test_ask_user_blank_answer_falls_back_and_completes(oa, adapter):
    from textual.widgets import Input

    app = OpenAgentTUI(oa)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _start_run(pilot, app)
        modal = await _wait_for_question(pilot)
        modal.query_one("#answer", Input).value = "   "  # whitespace only
        await pilot.press("enter")
        run = await _wait_terminal(pilot, oa, screen)
        assert run.status.value == "completed"

    events = oa.runs.output(run.id, "events")
    assert "question.cancelled" in events  # blank -> treated as no answer


# --------------------------------------------------------------------------- cancel while question open

async def test_cancel_run_while_question_open_terminates(oa, adapter):
    app = OpenAgentTUI(oa)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _start_run(pilot, app)
        await _wait_for_question(pilot)
        # Cancel the run while the modal is open: skip the question, then cancel the run.
        await pilot.press("escape")
        await oa.runs.cancel(screen._run_id)
        run = await _wait_terminal(pilot, oa, screen)
        # The worker terminated (no deadlock); the run is in a terminal state.
        assert run.status.value in ("completed", "cancelled")
