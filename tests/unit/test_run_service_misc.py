from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import Run, RunStatus
from openagent.security.process import capture_process_identity
from openagent.services.run_service import RunError


@pytest.fixture()
def app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    paths = Paths(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db",
        project_root=project,
    )
    return OpenAgentApp(paths)


def test_orphan_recovery_marks_dead_runs(app: OpenAgentApp):
    run = Run(id="run_dead", agent="x", status=RunStatus.RUNNING, pid=999999999)
    app.repos.runs.upsert(run)
    recovered = app.runs.recover_orphans()
    assert "run_dead" in recovered
    assert app.repos.runs.get("run_dead").status == RunStatus.ORPHANED


def test_orphan_recovery_fails_closed_on_unattached_live_run(app: OpenAgentApp):
    """A live PID this process does not own is orphaned, not left "running", and not killed (9.5).

    A restarted OpenAgent cannot reattach to a previous run's stdout/event stream, so a live PID it
    does not own (no CLI adapter, no cancellation controller) must fail closed: mark it orphaned,
    record the PID, and leave the process alone for the user to terminate.
    """
    import json
    import subprocess
    import sys

    import psutil

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        identity = capture_process_identity(proc.pid)
        assert identity is not None
        run = Run(
            id="run_live",
            agent="x",
            status=RunStatus.RUNNING,
            pid=proc.pid,
            pid_started_at=identity.create_time,
            process_identity=identity,
        )
        app.repos.runs.upsert(run)
        app.paths.run_dir(run.id).mkdir(
            parents=True, exist_ok=True
        )  # so the audit note can be written

        recovered = app.runs.recover_orphans()

        assert "run_live" in recovered
        reloaded = app.repos.runs.get("run_live")
        assert reloaded.status == RunStatus.ORPHANED
        assert reloaded.failure_type == "orphaned_unattached_process"
        # Fail-closed must never mean "kill a process we don't own": it is still alive.
        assert psutil.pid_exists(proc.pid)

        # The live PID and a "not terminated" note are recorded for the user (event/artifact).
        events = [
            json.loads(line)
            for line in (app.paths.run_dir(run.id) / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        orphan = next(e for e in events if e["data"].get("kind") == "orphan")
        assert orphan["data"]["pid"] == proc.pid
        assert orphan["data"]["killed"] is False
    finally:
        proc.kill()
        proc.wait()


def test_orphan_recovery_detects_pid_reuse(app: OpenAgentApp):
    """A live PID whose recorded start-time no longer matches is a *reused* PID — orphan it, and
    never act on the unrelated process (item 11)."""
    import subprocess
    import sys

    import psutil

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        identity = capture_process_identity(proc.pid)
        assert identity is not None
        wrong = identity.model_copy(update={"create_time": identity.create_time - 3600.0})
        run = Run(
            id="run_reused",
            agent="x",
            status=RunStatus.RUNNING,
            pid=proc.pid,
            pid_started_at=wrong.create_time,
            process_identity=wrong,
        )
        app.repos.runs.upsert(run)
        recovered = app.runs.recover_orphans()
        assert "run_reused" in recovered
        reloaded = app.repos.runs.get("run_reused")
        assert reloaded.status == RunStatus.ORPHANED
        assert reloaded.failure_type == "orphaned_pid_reused"
        # The unrelated process is untouched (still alive).
        assert psutil.pid_exists(proc.pid)
    finally:
        proc.kill()
        proc.wait()


def test_rebuild_artifacts_accumulates_provider_cost(app: OpenAgentApp):
    """Cumulative usage across turns sums provider_cost: turn1 cost + turn2 cost = total (item 12)."""
    from openagent.core.events import EventType, NormalizedEvent
    from openagent.storage.event_log import EventLog

    run = Run(id="run_cost", agent="x", status=RunStatus.RUNNING)
    app.repos.runs.upsert(run)
    log = EventLog(app.paths.run_dir(run.id), index=app.repos.event_index, run_id=run.id)
    for cost, inp in ((0.01, 10), (0.02, 3)):
        log.append(
            NormalizedEvent(
                run_id=run.id,
                type=EventType.USAGE_UPDATED,
                source="test",
                data={
                    "input_tokens": inp,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                    "provider_cost": cost,
                },
            )
        )
    art, _ = app.runs._rebuild_artifacts(run)
    assert art.usage["input_tokens"] == 13
    assert art.usage["provider_cost"] == 0.03


def test_rebuild_artifacts_cost_none_when_no_cost_reported(app: OpenAgentApp):
    """A CLI that reports no cost leaves provider_cost None, not 0 (item 12)."""
    from openagent.core.events import EventType, NormalizedEvent
    from openagent.storage.event_log import EventLog

    run = Run(id="run_nocost", agent="x", status=RunStatus.RUNNING)
    app.repos.runs.upsert(run)
    log = EventLog(app.paths.run_dir(run.id), index=app.repos.event_index, run_id=run.id)
    log.append(
        NormalizedEvent(
            run_id=run.id,
            type=EventType.USAGE_UPDATED,
            source="test",
            data={
                "input_tokens": 5,
                "cached_input_tokens": 0,
                "output_tokens": 2,
                "provider_cost": None,
            },
        )
    )
    art, _ = app.runs._rebuild_artifacts(run)
    assert art.usage["provider_cost"] is None


def test_output_unknown_format_raises(app: OpenAgentApp):
    with pytest.raises(RunError):
        app.runs.output("run_x", "bogus")


def test_output_missing_artifact_raises(app: OpenAgentApp):
    with pytest.raises(RunError):
        app.runs.output("run_missing", "json")


def test_create_run_unknown_agent(app: OpenAgentApp):
    with pytest.raises(RunError):
        app.runs.create(agent_name="nope", prompt="x")


def test_enum_value_renders_the_plain_status_not_the_repr():
    """`RunStatus` subclasses str, which makes the obvious guard a trap.

    `isinstance(RunStatus.RUNNING, str)` is True, so `x if isinstance(x, str) else x.value` returns
    the *enum* — and `str()` of it is "RunStatus.RUNNING". A live run showed exactly that in the Run
    Console header. Whether an f-string renders the value or the repr also varies by Python version,
    so every display/serialization path goes through this one helper instead.
    """

    from openagent.core.models import RunStatus, enum_value

    assert enum_value(RunStatus.RUNNING) == "running"
    assert enum_value(RunStatus.COMPLETED) == "completed"
    assert enum_value("already a string") == "already a string"
    assert "RunStatus" not in enum_value(RunStatus.FAILED)


async def test_run_console_header_shows_a_human_status(tmp_path):
    """The console header must say "running", never "RunStatus.RUNNING"."""

    import subprocess

    from textual.widgets import Static

    from openagent.app import OpenAgentApp
    from openagent.config import Paths
    from openagent.core.models import RuntimeType
    from openagent.tui.app import OpenAgentTUI
    from openagent.tui.screens.run_console import RunConsoleScreen
    from tests.fakecli import FakeCliAdapter, install_fake_cli, write_fake_script

    project = tmp_path / "proj"
    project.mkdir()

    def _git(*args):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)

    _git("init", "-q")
    _git("config", "user.email", "t@t.com")
    _git("config", "user.name", "t")
    (project / "seed.txt").write_text("seed\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "init")
    oa = OpenAgentApp(
        Paths(
            data_dir=tmp_path / "d",
            config_dir=tmp_path / "c",
            db_path=tmp_path / "d" / "o.db",
            project_root=project,
        )
    )
    oa.agents.create(name="fake-coder", runtime_type=RuntimeType.CLI, cli="fake")

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    try:
        install_fake_cli(mp, FakeCliAdapter(write_fake_script(tmp_path)))
        run = oa.runs.create(agent_name="fake-coder", prompt="go", worktree="auto")
        await oa.runs.execute(run)

        app = OpenAgentTUI(oa)
        async with app.run_test(size=(120, 40)) as pilot:
            app.push_screen(RunConsoleScreen(run.id))
            await pilot.pause(0.3)
            header = str(app.screen.query_one("#status", Static).content)
            assert "completed" in header
            assert "RunStatus" not in header
    finally:
        mp.undo()
