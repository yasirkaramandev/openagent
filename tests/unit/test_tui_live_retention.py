from __future__ import annotations

import time
from pathlib import Path

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.events import EventType, NormalizedEvent
from openagent.core.models import RuntimeType
from openagent.tui.app import LiveRun, OpenAgentTUI
from openagent.tui.screens.run_console import RunConsoleScreen


async def _settle(pilot, predicate, *, timeout: float = 3.0) -> None:
    """Pump the Textual event loop until ``predicate`` holds or ``timeout`` elapses.

    Layout/scroll changes settle over an unspecified number of refresh cycles, so a fixed pause is
    a race. This waits for the actual condition instead — the assertion after it still verifies the
    invariant, so a genuine regression fails rather than being masked.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)


def _event(run_id: str, index: int) -> NormalizedEvent:
    return NormalizedEvent(
        run_id=run_id,
        type=EventType.MESSAGE_COMPLETED,
        source="test",
        data={"item_id": f"m{index}", "text": f"message {index}"},
    )


def _oa(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "project"
    project.mkdir()
    app = OpenAgentApp(
        Paths(
            data_dir=tmp_path / "data",
            config_dir=tmp_path / "config",
            db_path=tmp_path / "data" / "openagent.db",
            project_root=project,
        )
    )
    app.agents.create(name="codex", runtime_type=RuntimeType.CLI, cli="codex")
    return app


def test_live_run_global_lru_budgets_and_terminal_retention(tmp_path: Path, monkeypatch) -> None:
    import openagent.tui.app as app_module

    monkeypatch.setattr(app_module, "MAX_LIVE_RUNS", 2)
    monkeypatch.setattr(app_module, "MAX_GLOBAL_LIVE_EVENTS", 3)
    tui = OpenAgentTUI(_oa(tmp_path))

    for run_index in range(3):
        live = LiveRun(f"run-{run_index}")
        for event_index in range(3):
            live.publish(_event(live.run_id, event_index))
        live.finish()
        live.last_access = float(run_index)
        tui.live_runs[live.run_id] = live

    tui._prune_live_runs()  # noqa: SLF001 - directly pins the cache contract

    assert len(tui.live_runs) == 2
    assert "run-0" not in tui.live_runs
    assert sum(len(live.events) for live in tui.live_runs.values()) == 3

    for live in tui.live_runs.values():
        live.finished_at = time.monotonic() - 301
    tui._prune_live_runs()  # noqa: SLF001
    assert tui.live_runs == {}


async def test_run_console_follows_only_while_user_is_at_end(tmp_path: Path) -> None:
    oa = _oa(tmp_path)
    run = oa.runs.create(agent_name="codex", prompt="test")
    tui = OpenAgentTUI(oa)

    async with tui.run_test(size=(70, 20)) as pilot:
        live = LiveRun(run.id, tui._prune_live_runs)  # noqa: SLF001
        tui.live_runs[run.id] = live
        for index in range(80):
            live.publish(_event(run.id, index))
        tui.push_screen(RunConsoleScreen(run.id))
        await pilot.pause()
        screen = tui.screen
        assert isinstance(screen, RunConsoleScreen)
        timeline = screen.query_one("#timeline")
        screen._scroll_output_to_end()  # noqa: SLF001
        assert timeline.scroll_y == timeline.max_scroll_y

        timeline.scroll_home(animate=False)
        screen.follow_output = False
        live.publish(_event(run.id, 81))
        await pilot.pause(0.3)
        assert timeline.scroll_y == 0

        screen.follow_output = True
        live.publish(_event(run.id, 82))
        # The follow-scroll settles over a Textual refresh cycle whose duration is not fixed — a
        # bounded `pause(0.3)` flaked on slower CI runners (py3.11). Poll until the layout settles.
        await _settle(pilot, lambda: timeline.scroll_y == timeline.max_scroll_y)
        assert timeline.scroll_y == timeline.max_scroll_y
