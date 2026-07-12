from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import Run, RunStatus
from openagent.services.run_service import RunError


@pytest.fixture()
def app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    paths = Paths(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db", project_root=project,
    )
    return OpenAgentApp(paths)


def test_orphan_recovery_marks_dead_runs(app: OpenAgentApp):
    run = Run(id="run_dead", agent="x", status=RunStatus.RUNNING, pid=999999999)
    app.repos.runs.upsert(run)
    recovered = app.runs.recover_orphans()
    assert "run_dead" in recovered
    assert app.repos.runs.get("run_dead").status == RunStatus.ORPHANED


def test_orphan_recovery_leaves_live_matching_run_running(app: OpenAgentApp):
    """A run whose PID is live *and* whose start-time matches is genuinely still running (item 11)."""
    import subprocess
    import sys

    import psutil

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        created = psutil.Process(proc.pid).create_time()
        run = Run(id="run_live", agent="x", status=RunStatus.RUNNING,
                  pid=proc.pid, pid_started_at=created)
        app.repos.runs.upsert(run)
        recovered = app.runs.recover_orphans()
        assert "run_live" not in recovered
        assert app.repos.runs.get("run_live").status == RunStatus.RUNNING
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
        created = psutil.Process(proc.pid).create_time()
        run = Run(id="run_reused", agent="x", status=RunStatus.RUNNING,
                  pid=proc.pid, pid_started_at=created - 3600.0)
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


def test_output_unknown_format_raises(app: OpenAgentApp):
    with pytest.raises(RunError):
        app.runs.output("run_x", "bogus")


def test_output_missing_artifact_raises(app: OpenAgentApp):
    with pytest.raises(RunError):
        app.runs.output("run_missing", "json")


def test_create_run_unknown_agent(app: OpenAgentApp):
    with pytest.raises(RunError):
        app.runs.create(agent_name="nope", prompt="x")
