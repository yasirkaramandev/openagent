"""Resume/follow-up obeys the SAME lifecycle contract as the first run (§4).

Every turn: exactly one terminal event, written LAST; a single outer exception boundary so any
failure (adapter build, backend, diff, or any artifact write) yields a terminal failed/cancelled turn
with a consistent, explicitly-partial bundle — never a run left "running" and never a success over a
failed artifact write. Concurrent follow-ups are rejected, not silently interleaved. Cancellation of
a resume really kills the process tree.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import RunStatus, RuntimeType, enum_value
from openagent.reporting.artifacts import ArtifactWriter
from openagent.security.process import is_pid_alive
from openagent.services.run_service import RunError
from openagent.storage.event_log import EventLog
from openagent.workspaces.worktree import WorktreeManager
from tests.fakecli import FakeCliAdapter, install_fake_cli, write_fake_script

_TERMINALS = {"run.completed", "run.failed", "run.cancelled"}


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture()
def app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    _git(["init", "-q"], project)
    _git(["config", "user.email", "t@t.com"], project)
    _git(["config", "user.name", "t"], project)
    (project / "seed.txt").write_text("seed\n")
    _git(["add", "-A"], project)
    _git(["commit", "-q", "-m", "init"], project)
    oa = OpenAgentApp(
        Paths(
            data_dir=tmp_path / "data",
            config_dir=tmp_path / "config",
            db_path=tmp_path / "data" / "openagent.db",
            project_root=project,
        )
    )
    oa.agents.create(name="fake-coder", runtime_type=RuntimeType.CLI, cli="fake")
    return oa


def _events(app: OpenAgentApp, run_id: str) -> list[dict]:
    return [
        json.loads(line) for line in app.runs.output(run_id, "events").splitlines() if line.strip()
    ]


def _terminals(app: OpenAgentApp, run_id: str) -> list[str]:
    return [e["type"] for e in _events(app, run_id) if e["type"] in _TERMINALS]


async def _first_turn(app: OpenAgentApp, tmp_path: Path, monkeypatch, resume_mode: str) -> str:
    adapter = FakeCliAdapter(write_fake_script(tmp_path), mode="complete", resume_mode=resume_mode)
    install_fake_cli(monkeypatch, adapter)
    run = app.runs.create(agent_name="fake-coder", prompt="first", worktree="auto")
    result = await app.runs.execute(run)
    assert result.status == RunStatus.COMPLETED
    return run.id


# --------------------------------------------------------------------------- terminal ordering


async def test_completed_resume_terminal_last(app, tmp_path, monkeypatch):
    run_id = await _first_turn(app, tmp_path, monkeypatch, "resume")
    resumed = await app.runs.resume(run_id, "second")
    assert resumed.status == RunStatus.COMPLETED
    events = _events(app, run_id)
    assert events[-1]["type"] == "run.completed", "the turn's terminal event must be last"
    assert _terminals(app, run_id) == ["run.completed", "run.completed"]  # one per turn


async def test_failed_resume_terminal_last(app, tmp_path, monkeypatch):
    run_id = await _first_turn(app, tmp_path, monkeypatch, "fail1")
    resumed = await app.runs.resume(run_id, "make it fail")
    assert resumed.status == RunStatus.FAILED
    events = _events(app, run_id)
    assert events[-1]["type"] == "run.failed"
    assert _terminals(app, run_id) == ["run.completed", "run.failed"]
    # The first turn's successful artifacts are preserved (cumulative), status is failed.
    assert json.loads(app.runs.output(run_id, "json"))["status"] == "failed"


async def test_cancelled_resume_terminal_last_and_kills_tree(app, tmp_path, monkeypatch):
    run_id = await _first_turn(app, tmp_path, monkeypatch, "longrun")

    task = asyncio.create_task(app.runs.resume(run_id, "long second"))
    pid = await _wait_for_turn_pid(app, run_id)
    assert is_pid_alive(pid)

    outcome = await app.runs.cancel(run_id)
    resumed = await task
    assert resumed.status == RunStatus.CANCELLED
    await asyncio.sleep(0.1)
    assert not is_pid_alive(pid), "the resumed turn's process tree must be terminated on cancel"

    events = _events(app, run_id)
    assert events[-1]["type"] == "run.cancelled"
    assert _terminals(app, run_id)[-1] == "run.cancelled"
    assert enum_value(outcome) in {"terminated", "signalled"}


async def _wait_for_turn_pid(app: OpenAgentApp, run_id: str, timeout: float = 5.0) -> int:
    """Wait for the *resumed* turn's live process (a fresh pid distinct from turn 1's dead one)."""

    adapter = app.runs._cli_adapters.get(run_id)  # noqa: SLF001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        proc = getattr(adapter, "_processes", {}).get(run_id) if adapter else None
        if proc is not None and proc.pid and is_pid_alive(proc.pid):
            return proc.pid
        await asyncio.sleep(0.02)
        adapter = app.runs._cli_adapters.get(run_id)  # noqa: SLF001
    raise AssertionError("resumed turn never reported a live pid")


# --------------------------------------------------------------------------- concurrency


async def test_concurrent_followup_rejected(app, tmp_path, monkeypatch):
    run_id = await _first_turn(app, tmp_path, monkeypatch, "longrun")
    task = asyncio.create_task(app.runs.resume(run_id, "long"))
    try:
        await _wait_for_turn_pid(app, run_id)
        # A second follow-up while one is running is rejected — never a silent second turn.
        with pytest.raises(RunError, match="a turn is already running"):
            await app.runs.resume(run_id, "sneak in")
        # The first turn's adapter/cancellation registry was not overwritten.
        assert app.runs._cli_adapters.get(run_id) is not None  # noqa: SLF001
    finally:
        await app.runs.cancel(run_id)
        await task


async def test_adapter_registry_not_overwritten_by_rejected_followup(app, tmp_path, monkeypatch):
    run_id = await _first_turn(app, tmp_path, monkeypatch, "longrun")
    task = asyncio.create_task(app.runs.resume(run_id, "long"))
    try:
        await _wait_for_turn_pid(app, run_id)
        original = app.runs._cli_adapters.get(run_id)  # noqa: SLF001
        with pytest.raises(RunError):
            await app.runs.resume(run_id, "again")
        assert app.runs._cli_adapters.get(run_id) is original  # noqa: SLF001
    finally:
        await app.runs.cancel(run_id)
        await task


# --------------------------------------------------------------------------- artifact failures


def _boom(*_a: object, **_k: object) -> None:
    raise OSError("injected resume artifact failure")


async def _resume_with_failure(app, tmp_path, monkeypatch, *, patch) -> str:
    """Turn 1 succeeds; then inject a failure and resume, asserting a consistent partial bundle."""

    run_id = await _first_turn(app, tmp_path, monkeypatch, "resume")
    patch()
    resumed = await app.runs.resume(run_id, "second")
    assert resumed.status == RunStatus.FAILED
    assert enum_value(app.runs.get(run_id).status) == "failed"
    return run_id


async def test_resume_write_turn_failure_recovers(app, tmp_path, monkeypatch):
    run_id = await _resume_with_failure(
        app,
        tmp_path,
        monkeypatch,
        patch=lambda: monkeypatch.setattr(ArtifactWriter, "write_turn", _boom),
    )
    assert json.loads(app.runs.output(run_id, "status"))["status"] == "failed"


async def test_resume_write_status_failure_recovers(app, tmp_path, monkeypatch):
    run_id = await _resume_with_failure(
        app,
        tmp_path,
        monkeypatch,
        patch=lambda: monkeypatch.setattr(ArtifactWriter, "write_status", _boom),
    )
    assert enum_value(app.runs.get(run_id).status) == "failed"


async def test_resume_write_results_failure_recovers(app, tmp_path, monkeypatch):
    run_id = await _resume_with_failure(
        app,
        tmp_path,
        monkeypatch,
        patch=lambda: monkeypatch.setattr(ArtifactWriter, "write_results", _boom),
    )
    status = json.loads(app.runs.output(run_id, "status"))
    assert status["status"] == "failed" and status.get("artifacts_partial") is True


async def test_resume_write_timeline_failure_recovers(app, tmp_path, monkeypatch):
    run_id = await _resume_with_failure(
        app,
        tmp_path,
        monkeypatch,
        patch=lambda: monkeypatch.setattr(ArtifactWriter, "write_timeline", _boom),
    )
    assert enum_value(app.runs.get(run_id).status) == "failed"


async def test_resume_diff_failure_recovers(app, tmp_path, monkeypatch):
    run_id = await _resume_with_failure(
        app,
        tmp_path,
        monkeypatch,
        patch=lambda: monkeypatch.setattr(WorktreeManager, "diff", _boom),
    )
    # A diff failure during finalization is its own terminal failure type.
    assert app.runs.get(run_id).failure_type in {"finalization_failed", "artifact_write_failed"}


async def test_resume_terminal_append_failure_leaves_consistent_bundle(app, tmp_path, monkeypatch):
    run_id = await _first_turn(app, tmp_path, monkeypatch, "resume")

    original = EventLog.append

    def append(self, event):
        etype = event.type if isinstance(event.type, str) else event.type.value
        if etype in _TERMINALS:
            raise OSError("injected append failure on resume terminal event")
        return original(self, event)

    monkeypatch.setattr(EventLog, "append", append)
    await app.runs.resume(run_id, "second")
    assert enum_value(app.runs.get(run_id).status) == "failed"
    # The terminal turn's failure is reconciled across artifacts and the bundle is flagged partial.
    status = json.loads(app.runs.output(run_id, "status"))
    assert status["status"] == "failed"
    assert status.get("artifacts_partial") is True
    timeline = (app.paths.run_dir(run_id) / "timeline.md").read_text()
    status_line = next(ln for ln in timeline.splitlines() if ln.startswith("- Status:"))
    assert "completed" not in status_line
