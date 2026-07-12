"""End-to-end TUI run + output flow with a fixture-backed CLI (spec §31, items 1/6/7).

Drives a real fixture run through the New Run screen and opens the artifact viewer, so the run
pipeline, live activity log, and Output/Diff/Logs/Result/Events tabs are all exercised in the UI.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import RuntimeType
from openagent.tui.app import OpenAgentTUI
from openagent.tui.screens.run_view import OutputScreen
from tests.fakecli import FakeCliAdapter, write_fake_script


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


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
    paths = Paths(data_dir=tmp_path / "data", config_dir=tmp_path / "config",
                  db_path=tmp_path / "data" / "openagent.db", project_root=project)
    app = OpenAgentApp(paths)
    app.agents.create(name="fake-coder", title="Fake Coder", runtime_type=RuntimeType.CLI,
                      cli="fake", description="a fixture agent")
    return app


@pytest.fixture()
def use_fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    adapter = FakeCliAdapter(write_fake_script(tmp_path), mode="complete")
    monkeypatch.setattr("openagent.services.run_service.build_cli_adapter",
                        lambda cli, executable=None: adapter)
    return adapter


async def test_run_through_new_run_screen_then_view_output(oa: OpenAgentApp, use_fake):
    app = OpenAgentTUI(oa)
    async with app.run_test(size=(120, 40)) as pilot:
        from textual.widgets import Input, Select

        app.open_section("new_run")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#agent", Select).value = "fake-coder"
        screen.query_one("#prompt", Input).value = "do the thing"
        await pilot.click("#run")

        # Wait for the threaded run worker to finish.
        for _ in range(200):
            await pilot.pause(0.05)
            run_id = getattr(screen, "_run_id", None)
            if run_id and oa.runs.get(run_id) and oa.runs.get(run_id).status.value in (
                "completed", "failed", "cancelled"
            ):
                break
        run_id = screen._run_id
        assert oa.runs.get(run_id).status.value == "completed"

        # The activity log streamed events.
        from textual.widgets import RichLog
        log_lines = "\n".join(str(line) for line in screen.query_one("#run-log", RichLog).lines)
        assert "completed" in log_lines.lower()

        # Open the artifact viewer and confirm every tab has content.
        app.push_screen(OutputScreen(run_id))
        await pilot.pause()
        out = app.screen
        assert isinstance(out, OutputScreen)
        from textual.widgets import TextArea

        # md/diff/json/events always have content; logs can be legitimately empty for a clean run
        # (it still loaded without a "no artifact" error).
        for fmt in ("md", "diff", "json", "events"):
            text = out.query_one(f"#log-{fmt}", TextArea).text
            assert text.strip(), f"{fmt} tab is empty"
        assert "no logs artifact" not in out.query_one("#log-logs", TextArea).text


async def test_output_screen_renders_completed_run(oa: OpenAgentApp, use_fake):
    run = oa.runs.create(agent_name="fake-coder", prompt="go", worktree="auto")
    await oa.runs.execute(run)

    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        app.push_screen(OutputScreen(run.id))
        await pilot.pause()
        from textual.widgets import TextArea

        assert "run.completed" in app.screen.query_one("#log-events", TextArea).text
        assert "new.txt" in app.screen.query_one("#log-diff", TextArea).text


async def test_no_secret_under_openagent_dir(oa: OpenAgentApp, use_fake):
    """After a run, nothing secret-looking is left under .openagent/ (verification item 10)."""
    run = oa.runs.create(agent_name="fake-coder", prompt="store sk-LEAK1234567890abcdEFGH please",
                         worktree="auto")
    await oa.runs.execute(run)
    state_dir = oa.paths.project_state_dir
    for path in state_dir.rglob("*"):
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            assert "sk-LEAK1234567890abcdEFGH" not in text, f"secret leaked into {path}"
