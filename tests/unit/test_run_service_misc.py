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


def test_output_unknown_format_raises(app: OpenAgentApp):
    with pytest.raises(RunError):
        app.runs.output("run_x", "bogus")


def test_output_missing_artifact_raises(app: OpenAgentApp):
    with pytest.raises(RunError):
        app.runs.output("run_missing", "json")


def test_create_run_unknown_agent(app: OpenAgentApp):
    with pytest.raises(RunError):
        app.runs.create(agent_name="nope", prompt="x")
