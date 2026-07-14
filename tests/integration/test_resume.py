"""Resume/message updates artifacts across turns and never erases prior work (spec §32, §45)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import Run, RunStatus, RuntimeType
from tests.fakecli import FakeCliAdapter, install_fake_cli, write_fake_script


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
    install_fake_cli(monkeypatch, adapter)
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
    install_fake_cli(monkeypatch, adapter)
    run = app.runs.create(agent_name="fake-coder", prompt="first", worktree="auto")
    await app.runs.execute(run)

    resumed = await app.runs.resume(run.id, "make it fail")
    assert resumed.status == RunStatus.FAILED
    # The earlier successful work is not erased: file + summary survive.
    assert "new.txt" in resumed.files_changed
    result_json = json.loads(app.runs.output(run.id, "json"))
    assert "did the thing" in result_json["summary"]


async def test_resume_accumulates_usage_and_scopes_turn_artifacts(app: OpenAgentApp, use_fake):
    run = app.runs.create(agent_name="fake-coder", prompt="first", worktree="auto")
    await app.runs.execute(run)
    result1 = json.loads(app.runs.output(run.id, "json"))
    turn1_in = result1["usage"]["input_tokens"]

    await app.runs.resume(run.id, "second")
    result2 = json.loads(app.runs.output(run.id, "json"))

    # Cumulative usage sums both turns (10 in turn 1 + 3 in turn 2 from the fake script).
    assert result2["usage"]["input_tokens"] == turn1_in + 3
    assert result2["usage"]["output_tokens"] == 5 + 2

    # turn_002.md is scoped to just the second turn's usage, not the cumulative total.
    turn2_md = (app.paths.run_dir(run.id) / "turn_002.md").read_text()
    assert "in 3 /" in turn2_md
    assert "## Prompt" in turn2_md and "second" in turn2_md
    assert "Events:" in turn2_md


async def test_successful_resume_clears_prior_failure_type(
    app: OpenAgentApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # First turn fails (but records a session), leaving a failure_type; a successful resume clears it.
    adapter = FakeCliAdapter(write_fake_script(tmp_path), mode="usage_limit", resume_mode="resume")
    install_fake_cli(monkeypatch, adapter)
    run = app.runs.create(agent_name="fake-coder", prompt="fail first", worktree="auto")
    failed = await app.runs.execute(run)
    assert failed.status == RunStatus.FAILED
    assert failed.failure_type is not None

    resumed = await app.runs.resume(run.id, "now succeed")
    assert resumed.status == RunStatus.COMPLETED
    assert resumed.failure_type is None
    result = json.loads(app.runs.output(run.id, "json"))
    assert result["status"] == "completed"


async def test_success_event_with_nonzero_exit_makes_run_failed(
    app: OpenAgentApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """End-to-end: a CLI that emits a success event but exits 1 yields a FAILED run (item 6)."""

    adapter = FakeCliAdapter(write_fake_script(tmp_path), mode="success_exit1")
    install_fake_cli(monkeypatch, adapter)
    run = app.runs.create(agent_name="fake-coder", prompt="go", worktree="auto")
    result = await app.runs.execute(run)
    assert result.status == RunStatus.FAILED
    # Exactly one terminal event in the log.
    events = app.runs.output(run.id, "events")
    terminals = sum(events.count(t) for t in ("run.completed", "run.failed", "run.cancelled"))
    assert terminals == 1, events


async def test_pid_and_session_persisted_immediately(app: OpenAgentApp, use_fake):
    """The pid and session id must hit the DB **the moment they arrive**, not at the end.

    That is the whole point: a crash halfway through a run must still leave something another
    process can cancel (needs the pid) and resume (needs the session id). Asserting only after the
    run finishes would pass even if both were written once, at the very end — which is exactly the
    hole this checks for.
    """

    run = app.runs.create(agent_name="fake-coder", prompt="task", worktree="auto")

    # Read the *persisted* row at the instant each event is emitted.
    at_event: dict[str, tuple] = {}

    def on_event(event) -> None:
        etype = event.type if isinstance(event.type, str) else event.type.value
        if etype in ("process.started", "session.created"):
            stored = app.repos.runs.get(run.id)
            at_event[etype] = (stored.pid, stored.provider_session_id)

    await app.runs.execute(run, on_event=on_event)

    # The pid was durable as soon as the backend process came up…
    assert at_event["process.started"][0] is not None, "pid was not persisted when it arrived"
    # …and the session id as soon as the backend reported it.
    assert at_event["session.created"][1] == "th-fake-1", (
        "session id was not persisted when it arrived"
    )

    assert app.runs.get(run.id).provider_session_id == "th-fake-1"

    # A brand-new app object (simulating a restart) reads the same persisted state and can resume.
    restarted = OpenAgentApp(app.paths)
    reloaded = restarted.runs.get(run.id)
    assert reloaded is not None
    assert reloaded.provider_session_id == "th-fake-1"
    assert reloaded.pid is not None


def test_orphan_recovery_uses_pid_identity(app: OpenAgentApp):
    # A run left "running" with a dead PID is recovered as orphaned on restart.
    run = Run(id="run_orphan", agent="fake-coder", status=RunStatus.RUNNING, pid=2_000_000_000)
    app.repos.runs.upsert(run)
    recovered = app.runs.recover_orphans()
    assert "run_orphan" in recovered
    assert app.runs.get("run_orphan").status == RunStatus.ORPHANED
