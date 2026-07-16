"""Full end-to-end TUI ask_user flow, including real cancellation from a modal (items 9, 14, 22).

Drives a real API-agent run through the Run Console: the run executes as an app-level worker, a mocked
model calls ``ask_user`` on turn 1, the QuestionModal appears, the pilot types an answer, the modal
closes, the answer reaches the second model request, the run completes, the console updates, and the
event stream carries question.requested/question.answered. Also covers Esc/skip, a blank answer, and —
the case the old suite got wrong — **Ctrl+C while the question is open**, which must cancel the whole
run: status ``cancelled``, the worker stopped, the provider never called again, and no later
``run.completed``. "Completed" is not an acceptable outcome for a cancellation.
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
            yield NormalizedModelEvent(
                type=ModelEventType.TEXT_DELTA, text="Understood — proceeding with your answer."
            )
            yield NormalizedModelEvent(type=ModelEventType.DONE)


@pytest.fixture()
def oa(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    app = OpenAgentApp(
        Paths(
            data_dir=tmp_path / "data",
            config_dir=tmp_path / "config",
            db_path=tmp_path / "data" / "openagent.db",
            project_root=project,
        )
    )
    app.providers.add(
        name="testco", provider_type="custom", base_url="https://api.test/v1", api_key="sk-x"
    )
    app.agents.create(
        name="asker", runtime_type=RuntimeType.API_AGENT, provider="testco", model="test-model"
    )
    return app


@pytest.fixture()
def adapter(oa: OpenAgentApp, monkeypatch: pytest.MonkeyPatch) -> AskingAdapter:
    ad = AskingAdapter()
    monkeypatch.setattr(oa.providers, "adapter_for", lambda provider: ad)
    return ad


async def _start_run(pilot, app) -> str:
    """Drive the setup screen and return the run id the console opened on."""

    from textual.widgets import Input, Select

    app.open_section("new_run")
    await pilot.pause()
    screen = app.screen
    screen.query_one("#agent", Select).value = "asker"
    screen.query_one("#prompt", Input).value = "set up the server"
    await pilot.click("#run")
    for _ in range(200):
        await pilot.pause(0.05)
        runs = app.oa.runs.list(1)
        if runs:
            return runs[0].id
    raise AssertionError("the run never started")


async def _wait_for_question(pilot, timeout: int = 200) -> QuestionModal:
    for _ in range(timeout):
        await pilot.pause(0.05)
        if isinstance(pilot.app.screen, QuestionModal):
            return pilot.app.screen
    raise AssertionError("QuestionModal never appeared")


async def _wait_terminal(pilot, oa, run_id: str, timeout: int = 400):
    for _ in range(timeout):
        await pilot.pause(0.05)
        run = oa.runs.get(run_id)
        if run and run.status.value in ("completed", "failed", "cancelled"):
            return run
    raise AssertionError("run worker did not terminate (possible deadlock)")


def _terminal_events(oa, run_id: str) -> list[str]:
    """Every terminal event actually written to the log — there must be exactly one."""

    out = []
    for line in oa.runs.output(run_id, "events").splitlines():
        compact = line.replace(" ", "")
        for kind in ("run.completed", "run.failed", "run.cancelled"):
            if f'"type":"{kind}"' in compact:
                out.append(kind)
    return out


# --------------------------------------------------------------------------- happy path


async def test_ask_user_modal_answer_reaches_model_and_completes(oa, adapter):
    from textual.widgets import Input

    app = OpenAgentTUI(oa)
    async with app.run_test(size=(120, 40)) as pilot:
        run_id = await _start_run(pilot, app)
        modal = await _wait_for_question(pilot)
        assert "which port" in str(modal.query_one("#q").content)

        # Type an answer and submit (Enter).
        modal.query_one("#answer", Input).value = "8080"
        await pilot.press("enter")

        run = await _wait_terminal(pilot, oa, run_id)
        assert run.status.value == "completed"

        # The console projected the run to completion.
        await pilot.pause(0.3)
        assert app.live_run(run_id).projection.status == "completed"

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
        run_id = await _start_run(pilot, app)
        await _wait_for_question(pilot)
        await pilot.press("escape")  # Esc *skips* the question; the run carries on
        run = await _wait_terminal(pilot, oa, run_id)
        assert run.status.value == "completed"  # best-judgment fallback, no deadlock

    events = oa.runs.output(run.id, "events")
    assert "question.requested" in events
    assert "question.cancelled" in events


# --------------------------------------------------------------------------- blank answer


async def test_ask_user_blank_answer_falls_back_and_completes(oa, adapter):
    from textual.widgets import Input

    app = OpenAgentTUI(oa)
    async with app.run_test(size=(120, 40)) as pilot:
        run_id = await _start_run(pilot, app)
        modal = await _wait_for_question(pilot)
        modal.query_one("#answer", Input).value = "   "  # whitespace only
        await pilot.press("enter")
        run = await _wait_terminal(pilot, oa, run_id)
        assert run.status.value == "completed"

    events = oa.runs.output(run.id, "events")
    assert "question.cancelled" in events  # blank -> treated as no answer


# ------------------------------------------------------ Ctrl+C while the question is open (item 9)


async def test_ctrl_c_in_the_question_modal_cancels_the_whole_run(oa, adapter):
    """Ctrl+C in a QuestionModal must cancel the **run**, not merely skip the question (item 9).

    This is the case the old test got wrong: it pressed Esc, called cancel, and then accepted
    ``completed`` as a pass. But an API run had no way to learn it had been cancelled, so the agent
    loop simply carried on and finished — the run was never really stopped. Now the app raises the
    cancellation flag *before* releasing the modal, so the unblocked worker finds the run already
    cancelled and stops at its next checkpoint.
    """

    app = OpenAgentTUI(oa)
    async with app.run_test(size=(120, 40)) as pilot:
        run_id = await _start_run(pilot, app)
        await _wait_for_question(pilot)
        calls_when_blocked = adapter.calls

        await pilot.press("ctrl+c")

        run = await _wait_terminal(pilot, oa, run_id)
        assert run.status.value == "cancelled", "the run was not really cancelled"
        assert run.failure_type == "user_cancelled"

        # The modal is gone and the worker is done — no deadlock, no lingering screen.
        assert not isinstance(app.screen, QuestionModal)
        for _ in range(100):
            await pilot.pause(0.05)
            if app.live_run(run_id).finished:
                break
        assert app.live_run(run_id).finished, "the run worker never stopped"

    # The agent loop stopped instead of continuing: the provider was never asked for another turn.
    assert adapter.calls == calls_when_blocked, "the model was called again after the cancel"

    # Exactly one terminal event, and it is the cancellation — never a later completed.
    terminals = _terminal_events(oa, run_id)
    assert terminals == ["run.cancelled"], f"expected one run.cancelled, got {terminals}"


async def test_cancel_button_cancels_an_api_run_mid_stream(oa, adapter):
    """Cancelling an API run stops it even with no process to kill (item 9)."""

    app = OpenAgentTUI(oa)
    async with app.run_test(size=(120, 40)) as pilot:
        run_id = await _start_run(pilot, app)
        await _wait_for_question(pilot)
        app.cancel_active_run(run_id)

        run = await _wait_terminal(pilot, oa, run_id)
        assert run.status.value == "cancelled"

    assert _terminal_events(oa, run_id) == ["run.cancelled"]
