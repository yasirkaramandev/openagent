"""The Run Console, driven end to end through the real TUI (items 2, 10, 11, 22).

A fixture-backed CLI produces a real run — real subprocess, real events, real artifacts — and the
console is exercised the way a user would: set up, preflight, run, watch, leave, reopen.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from textual.widgets import Button, Input, Select, Static, TextArea

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import RuntimeType
from openagent.tui.app import OpenAgentTUI
from openagent.tui.screens.run_console import RunConsoleScreen, RunSetupScreen
from tests.fakecli import FakeCliAdapter, install_fake_cli, write_fake_script

TERMINAL = ("completed", "failed", "cancelled")


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _terminal_lines(oa: OpenAgentApp, run_id: str) -> list[str]:
    return [
        line
        for line in oa.runs.output(run_id, "events").splitlines()
        if '"type":"run.completed"' in line.replace(" ", "")
        or '"type":"run.failed"' in line.replace(" ", "")
        or '"type":"run.cancelled"' in line.replace(" ", "")
    ]


@pytest.fixture()
def oa(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    _git(["init", "-q"], project)
    _git(["config", "user.email", "t@t.com"], project)
    _git(["config", "user.name", "t"], project)
    (project / "seed.txt").write_text("seed\n")
    _git(["add", "-A"], project)
    _git(["commit", "-q", "-m", "init"], project)
    paths = Paths(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db",
        project_root=project,
    )
    app = OpenAgentApp(paths)
    app.agents.create(
        name="fake-coder",
        title="Fake Coder",
        runtime_type=RuntimeType.CLI,
        cli="fake",
        description="a fixture agent",
    )
    return app


@pytest.fixture()
def use_fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    adapter = FakeCliAdapter(write_fake_script(tmp_path), mode="complete")
    install_fake_cli(monkeypatch, adapter)
    return adapter


async def _await_terminal(oa: OpenAgentApp, pilot, run_id: str, tries: int = 300) -> str:
    for _ in range(tries):
        await pilot.pause(0.05)
        run = oa.runs.get(run_id)
        if run is not None:
            status = run.status if isinstance(run.status, str) else run.status.value
            # The row reserves its terminal status before the worker flushes artifacts and the
            # terminal JSONL event. Tests that inspect the bundle must wait for both boundaries.
            if status in TERMINAL and _terminal_lines(oa, run_id):
                return status
    raise AssertionError(f"run {run_id} never reached a terminal status")


def _latest_run(oa: OpenAgentApp):
    runs = oa.runs.list(1)
    return runs[0] if runs else None


async def test_setup_screen_shows_runtime_and_readiness(oa: OpenAgentApp, use_fake):
    """Selecting an agent immediately shows what it *is*, and readiness can be checked (item 2)."""

    app = OpenAgentTUI(oa)
    async with app.run_test(size=(120, 40)) as pilot:
        app.open_section("new_run")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, RunSetupScreen)

        screen.query_one("#agent", Select).value = "fake-coder"
        await pilot.pause()
        info = str(screen.query_one("#agent-info", Static).content)
        assert "Runtime: CLI" in info
        assert "fake" in info

        await pilot.click("#check")
        for _ in range(100):
            await pilot.pause(0.05)
            text = str(screen.query_one("#preflight", Static).content)
            if "Ready to run" in text or "Not ready" in text:
                break
        assert "Ready to run" in str(screen.query_one("#preflight", Static).content)


async def test_run_streams_into_the_console_and_reaches_completed(oa: OpenAgentApp, use_fake):
    app = OpenAgentTUI(oa)
    async with app.run_test(size=(120, 40)) as pilot:
        app.open_section("new_run")
        await pilot.pause()
        app.screen.query_one("#agent", Select).value = "fake-coder"
        app.screen.query_one("#prompt", Input).value = "do the thing"
        await pilot.click("#run")

        # Preflight runs, then the console replaces the setup form.
        for _ in range(200):
            await pilot.pause(0.05)
            if isinstance(app.screen, RunConsoleScreen):
                break
        console = app.screen
        assert isinstance(console, RunConsoleScreen), "the console never opened"

        status = await _await_terminal(oa, pilot, console.run_id)
        assert status == "completed"

        await pilot.pause(0.3)
        projection = console.projection
        assert projection.final_message == "did the thing"
        assert any(i.path == "new.txt" for i in projection.files)

        # The status header reflects the real outcome.
        assert "completed" in str(console.query_one("#status", Static).content)


async def test_leaving_the_console_does_not_kill_the_run(
    oa: OpenAgentApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Closing the console must not cancel the agent — the run is owned by the app (item 10).

    The run is executed as an app-level worker precisely so that popping the screen cannot take it
    down with it. A long-running fake proves the process is still alive after we leave.
    """

    adapter = FakeCliAdapter(write_fake_script(tmp_path), mode="longrun")
    install_fake_cli(monkeypatch, adapter)

    app = OpenAgentTUI(oa)
    async with app.run_test(size=(100, 30)) as pilot:
        app.open_section("new_run")
        await pilot.pause()
        app.screen.query_one("#agent", Select).value = "fake-coder"
        app.screen.query_one("#prompt", Input).value = "long task"
        await pilot.click("#run")

        for _ in range(200):
            await pilot.pause(0.05)
            if isinstance(app.screen, RunConsoleScreen):
                break
        console = app.screen
        run_id = console.run_id

        # Wait until the backend process is actually up.
        for _ in range(200):
            await pilot.pause(0.05)
            if oa.runs.get(run_id).pid:
                break
        assert oa.runs.get(run_id).pid, "the backend never started"

        # Leave the console.
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, RunConsoleScreen)

        # The run is still going: still live, still not terminal.
        await pilot.pause(0.3)
        run = oa.runs.get(run_id)
        status = run.status if isinstance(run.status, str) else run.status.value
        assert status not in TERMINAL, "leaving the console killed the run"
        assert app.live_run(run_id) is not None

        # Reopen it from the Runs screen: state is replayed and updates keep arriving.
        app.open_section("runs")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.2)
        reopened = app.screen
        assert isinstance(reopened, RunConsoleScreen)
        assert reopened.run_id == run_id
        assert reopened.projection.pid, "the reopened console did not replay the event log"

        # Clean up: cancel the long-running process.
        app.cancel_active_run(run_id)
        await _await_terminal(oa, pilot, run_id)


async def test_cancel_from_the_console_really_stops_the_run(
    oa: OpenAgentApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    adapter = FakeCliAdapter(write_fake_script(tmp_path), mode="longrun")
    install_fake_cli(monkeypatch, adapter)

    app = OpenAgentTUI(oa)
    async with app.run_test(size=(100, 30)) as pilot:
        app.open_section("new_run")
        await pilot.pause()
        app.screen.query_one("#agent", Select).value = "fake-coder"
        app.screen.query_one("#prompt", Input).value = "long task"
        await pilot.click("#run")

        for _ in range(200):
            await pilot.pause(0.05)
            if isinstance(app.screen, RunConsoleScreen):
                break
        run_id = app.screen.run_id
        for _ in range(200):
            await pilot.pause(0.05)
            if oa.runs.get(run_id).pid:
                break

        await pilot.click("#cancel")
        status = await _await_terminal(oa, pilot, run_id)
        assert status == "cancelled"

        # Exactly one terminal event, and it is the cancellation — never a later "completed".
        terminals = _terminal_lines(oa, run_id)
        assert len(terminals) == 1
        assert "run.cancelled" in terminals[0]


async def test_console_action_bar_survives_small_terminals(oa: OpenAgentApp, use_fake):
    """The action bar must stay visible at 80x24, 100x30 and 120x40 (item 2)."""

    for size in ((80, 24), (100, 30), (120, 40)):
        app = OpenAgentTUI(oa)
        run = oa.runs.create(agent_name="fake-coder", prompt="go", worktree="auto")
        await oa.runs.execute(run)
        async with app.run_test(size=size) as pilot:
            app.push_screen(RunConsoleScreen(run.id))
            await pilot.pause()
            console = app.screen
            actions = console.query_one("#actions")
            assert actions.region.height > 0, f"action bar collapsed at {size}"
            assert actions.region.y + actions.region.height <= size[1], (
                f"action bar pushed off-screen at {size}"
            )
            for button in ("#cancel", "#follow", "#back"):
                assert console.query_one(button, Button).region.width > 0, (
                    f"{button} not rendered at {size}"
                )


async def test_console_reopens_a_finished_run_from_the_event_log(oa: OpenAgentApp, use_fake):
    run = oa.runs.create(agent_name="fake-coder", prompt="go", worktree="auto")
    await oa.runs.execute(run)

    app = OpenAgentTUI(oa)
    async with app.run_test(size=(120, 40)) as pilot:
        app.push_screen(RunConsoleScreen(run.id))
        await pilot.pause(0.3)
        console = app.screen

        # Everything is reconstructed purely from events.jsonl — no live worker involved.
        assert console.projection.final_message == "did the thing"
        assert console.projection.session_id == "th-fake-1"
        assert "new.txt" in console.query_one("#pane-diff", TextArea).text
        assert "run.completed" in console.query_one("#pane-raw", TextArea).text


async def test_no_secret_under_openagent_dir(oa: OpenAgentApp, use_fake):
    """After a run, nothing secret-looking is left under .openagent/ (verification item 10)."""
    run = oa.runs.create(
        agent_name="fake-coder", prompt="store sk-LEAK1234567890abcdEFGH please", worktree="auto"
    )
    await oa.runs.execute(run)
    state_dir = oa.paths.project_state_dir
    for path in state_dir.rglob("*"):
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            assert "sk-LEAK1234567890abcdEFGH" not in text, f"secret leaked into {path}"
