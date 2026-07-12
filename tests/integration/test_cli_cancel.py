"""CLI cancellation actually terminates the process tree and records a cancelled run (spec §45).

Uses a real long-running subprocess via the fake adapter, driven through the full RunService so the
app-scoped adapter registry, immediate PID persistence, process-tree kill, ``run.cancelled`` event,
and idempotency are all exercised.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import RunStatus, RuntimeType
from openagent.security.process import is_pid_alive
from tests.fakecli import FakeCliAdapter, write_fake_script


@pytest.fixture()
def app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    paths = Paths(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db", project_root=project,
    )
    oa = OpenAgentApp(paths)
    oa.agents.create(name="fake-coder", runtime_type=RuntimeType.CLI, cli="fake",
                     permission_profile="safe-edit")
    return oa


@pytest.fixture()
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCliAdapter:
    script = write_fake_script(tmp_path)
    adapter = FakeCliAdapter(script, mode="longrun")
    monkeypatch.setattr(
        "openagent.services.run_service.build_cli_adapter",
        lambda cli, executable=None: adapter,
    )
    return adapter


async def _wait_for_pid(app: OpenAgentApp, run_id: str, timeout: float = 5.0) -> int:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        run = app.runs.get(run_id)
        if run and run.pid:
            return run.pid
        await asyncio.sleep(0.02)
    raise AssertionError("pid was never persisted")


async def test_cancel_terminates_process_and_marks_cancelled(app: OpenAgentApp, fake):
    run = app.runs.create(agent_name="fake-coder", prompt="do a long thing", worktree="auto")
    task = asyncio.create_task(app.runs.execute(run))

    pid = await _wait_for_pid(app, run.id)
    assert is_pid_alive(pid)
    # PID identity was captured immediately for safe later termination (spec §45).
    assert app.runs.get(run.id).pid_started_at is not None

    await app.runs.cancel(run.id)
    result = await task

    assert result.status == RunStatus.CANCELLED
    assert app.runs.get(run.id).status == RunStatus.CANCELLED
    # The whole process tree is gone.
    await asyncio.sleep(0.1)
    assert not is_pid_alive(pid)
    # A cancelled run must record run.cancelled and not be overwritten by completed.
    events = app.runs.output(run.id, "events")
    assert "run.cancelled" in events
    assert "run.completed" not in events


async def test_cancel_is_idempotent(app: OpenAgentApp, fake):
    run = app.runs.create(agent_name="fake-coder", prompt="x", worktree="auto")
    task = asyncio.create_task(app.runs.execute(run))
    await _wait_for_pid(app, run.id)
    await app.runs.cancel(run.id)
    await task
    # A second cancel on an already-terminal run is a no-op, not an error.
    await app.runs.cancel(run.id)
    assert app.runs.get(run.id).status == RunStatus.CANCELLED


async def test_completed_run_not_marked_cancelled(app: OpenAgentApp, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch):
    adapter = FakeCliAdapter(write_fake_script(tmp_path), mode="complete")
    monkeypatch.setattr("openagent.services.run_service.build_cli_adapter",
                        lambda cli, executable=None: adapter)
    run = app.runs.create(agent_name="fake-coder", prompt="quick", worktree="auto")
    result = await app.runs.execute(run)
    assert result.status == RunStatus.COMPLETED
    assert "run.completed" in app.runs.output(run.id, "events")
