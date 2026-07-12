"""Resume/message updates artifacts across turns and never erases prior work (spec §32, §45)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import Run, RunStatus, RuntimeType
from tests.fakecli import FakeCliAdapter, write_fake_script


@pytest.fixture()
def app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    import subprocess

    def git(*args):
        subprocess.run(["git", *args], cwd=str(project), check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t.com")
    git("config", "user.name", "t")
    (project / "seed.txt").write_text("seed\n")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    paths = Paths(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db", project_root=project,
    )
    oa = OpenAgentApp(paths)
    oa.agents.create(name="fake-coder", runtime_type=RuntimeType.CLI, cli="fake")
    return oa


@pytest.fixture()
def use_fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCliAdapter:
    adapter = FakeCliAdapter(write_fake_script(tmp_path), mode="complete", resume_mode="resume")
    monkeypatch.setattr("openagent.services.run_service.build_cli_adapter",
                        lambda cli, executable=None: adapter)
    return adapter


async def test_resume_accumulates_turns_and_artifacts(app: OpenAgentApp, use_fake):
    run = app.runs.create(agent_name="fake-coder", prompt="first task", worktree="auto")
    result = await app.runs.execute(run)
    assert result.status == RunStatus.COMPLETED
    assert "new.txt" in result.files_changed

    resumed = await app.runs.resume(run.id, "second task")
    assert resumed.status == RunStatus.COMPLETED
    assert resumed.turns == 2

    # Both turns' files are present in the cumulative changed set + diff.
    assert "new.txt" in resumed.files_changed and "second.txt" in resumed.files_changed
    diff = app.runs.output(run.id, "diff")
    assert "new.txt" in diff and "second.txt" in diff

    # result.json reflects the latest turn's summary and the turn count.
    result_json = json.loads(app.runs.output(run.id, "json"))
    assert result_json["turns"] == 2
    assert "second turn done" in result_json["summary"]

    # A per-turn artifact was written and earlier events preserved.
    assert (app.paths.run_dir(run.id) / "turn_002.md").exists()
    events = app.runs.output(run.id, "events")
    assert events.count("run.completed") == 2  # one per turn, earlier one preserved


async def test_failed_resume_preserves_earlier_artifacts(app: OpenAgentApp, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch):
    adapter = FakeCliAdapter(write_fake_script(tmp_path), mode="complete", resume_mode="fail1")
    monkeypatch.setattr("openagent.services.run_service.build_cli_adapter",
                        lambda cli, executable=None: adapter)
    run = app.runs.create(agent_name="fake-coder", prompt="first", worktree="auto")
    await app.runs.execute(run)

    resumed = await app.runs.resume(run.id, "make it fail")
    assert resumed.status == RunStatus.FAILED
    # The earlier successful work is not erased: file + summary survive.
    assert "new.txt" in resumed.files_changed
    result_json = json.loads(app.runs.output(run.id, "json"))
    assert "did the thing" in result_json["summary"]


async def test_pid_and_session_persisted_immediately(app: OpenAgentApp, use_fake):
    """Crash-restart simulation: after a run, a fresh app instance sees the persisted session id."""
    run = app.runs.create(agent_name="fake-coder", prompt="task", worktree="auto")
    await app.runs.execute(run)
    assert app.runs.get(run.id).provider_session_id == "th-fake-1"

    # A brand-new app object (simulating a restart) reads the same persisted state and can resume.
    restarted = OpenAgentApp(app.paths)
    reloaded = restarted.runs.get(run.id)
    assert reloaded is not None
    assert reloaded.provider_session_id == "th-fake-1"


def test_orphan_recovery_uses_pid_identity(app: OpenAgentApp):
    # A run left "running" with a dead PID is recovered as orphaned on restart.
    run = Run(id="run_orphan", agent="fake-coder", status=RunStatus.RUNNING, pid=2_000_000_000)
    app.repos.runs.upsert(run)
    recovered = app.runs.recover_orphans()
    assert "run_orphan" in recovered
    assert app.runs.get("run_orphan").status == RunStatus.ORPHANED
