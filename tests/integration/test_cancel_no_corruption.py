"""A cancel that stops nothing must change nothing (spec §6).

The cross-process cancel path (no live adapter/controller in this process — e.g. after a restart)
used to terminate by PID and then persist ``cancelled`` **unconditionally**::

    killed = terminate_pid_tree(run.pid, run.pid_started_at)
    self._persist_cancelled(run, reason, "user_cancelled")          # <- ran even when killed is False
    return CancelOutcome.TERMINATED if killed else CancelOutcome.IDENTITY_MISMATCH

So when the identity check refused to kill (PID reused by an unrelated process, PID unverifiable,
process already gone), OpenAgent still wrote ``run.cancelled`` to the event log, flipped the DB to
``cancelled`` and rewrote ``status.json`` — while the *real* backend process, if any, kept running.
The recorded history became fiction: it claimed a cancellation that never happened.

``run.cancelled`` may only be written when a process was really terminated, or when an in-process
cancellation controller really received the signal.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.events import EventType, NormalizedEvent
from openagent.core.models import Run, RunStatus
from openagent.security.process import is_pid_alive, pid_identity, terminate_process_tree
from openagent.services.run_service import CancelOutcome
from openagent.storage.event_log import EventLog

_SLEEPER = "import time; time.sleep(120)"


@pytest.fixture()
def app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    return OpenAgentApp(
        Paths(
            data_dir=tmp_path / "data",
            config_dir=tmp_path / "config",
            db_path=tmp_path / "data" / "openagent.db",
            project_root=project,
        )
    )


def _seed(app: OpenAgentApp, pid: int | None, create_time: float | None) -> Run:
    """A RUNNING run this process does not own, with a realistic run_dir."""

    run = Run(
        id="run_xproc",
        agent="ghost",
        status=RunStatus.RUNNING,
        pid=pid,
        pid_started_at=create_time,
    )
    app.repos.runs.upsert(run)
    run_dir = app.paths.run_dir(run.id)
    run_dir.mkdir(parents=True, exist_ok=True)
    EventLog(run_dir, index=app.repos.event_index).append(
        NormalizedEvent(run_id=run.id, type=EventType.RUN_STARTED, source="openagent", data={})
    )
    return run


def _snapshot(app: OpenAgentApp, run_id: str) -> tuple[str, str]:
    run_dir = app.paths.run_dir(run_id)
    events = (run_dir / "events.jsonl").read_text()
    status = (run_dir / "status.json").read_text() if (run_dir / "status.json").exists() else ""
    return events, status


async def test_reused_pid_cancel_leaves_state_untouched(app: OpenAgentApp):
    """The PID is alive but belongs to someone else: refuse, and record nothing."""

    proc = subprocess.Popen([sys.executable, "-c", _SLEEPER], start_new_session=True)  # noqa: S603
    try:
        # A create-time that does not match the live process → PID_REUSED.
        _seed(app, proc.pid, (pid_identity(proc.pid) or time.time()) - 10_000.0)
        before_events, before_status = _snapshot(app, "run_xproc")

        outcome = await app.runs.cancel("run_xproc")

        assert outcome is CancelOutcome.IDENTITY_MISMATCH
        # The unrelated process is untouched.
        assert is_pid_alive(proc.pid)
        # The DB was NOT rewritten to a cancellation that never happened.
        run = app.runs.get("run_xproc")
        assert run.status == RunStatus.RUNNING, (
            "a refused cancel must not flip the run to cancelled"
        )
        assert run.completed_at is None
        # No fabricated terminal event, and no rewritten artifacts.
        after_events, after_status = _snapshot(app, "run_xproc")
        assert after_events == before_events, "a refused cancel must not append run.cancelled"
        assert "run.cancelled" not in after_events
        assert after_status == before_status
    finally:
        terminate_process_tree(proc.pid)


async def test_unknown_pid_cancel_leaves_state_untouched(app: OpenAgentApp):
    """A live PID with no recorded create-time is unverifiable → refuse, record nothing."""

    proc = subprocess.Popen([sys.executable, "-c", _SLEEPER], start_new_session=True)  # noqa: S603
    try:
        _seed(app, proc.pid, None)
        before_events, _ = _snapshot(app, "run_xproc")

        outcome = await app.runs.cancel("run_xproc")

        assert outcome is CancelOutcome.IDENTITY_MISMATCH
        assert is_pid_alive(proc.pid)
        assert app.runs.get("run_xproc").status == RunStatus.RUNNING
        assert _snapshot(app, "run_xproc")[0] == before_events
        assert "run.cancelled" not in _snapshot(app, "run_xproc")[0]
    finally:
        terminate_process_tree(proc.pid)


async def test_already_gone_pid_cancel_leaves_state_untouched(app: OpenAgentApp):
    """Nothing was stopped because nothing was running — do not claim a cancellation."""

    _seed(app, 2_000_000_000, 1.0)  # a PID that does not exist
    before_events, _ = _snapshot(app, "run_xproc")

    outcome = await app.runs.cancel("run_xproc")

    assert outcome is CancelOutcome.IDENTITY_MISMATCH
    assert app.runs.get("run_xproc").status == RunStatus.RUNNING
    assert _snapshot(app, "run_xproc")[0] == before_events
    assert "run.cancelled" not in _snapshot(app, "run_xproc")[0]


async def test_no_pid_recorded_cancel_leaves_state_untouched(app: OpenAgentApp):
    _seed(app, None, None)
    before_events, _ = _snapshot(app, "run_xproc")

    outcome = await app.runs.cancel("run_xproc")

    assert outcome is CancelOutcome.IDENTITY_MISMATCH
    assert app.runs.get("run_xproc").status == RunStatus.RUNNING
    assert _snapshot(app, "run_xproc")[0] == before_events


async def test_real_termination_does_persist_cancelled(app: OpenAgentApp):
    """The positive case must still work: a genuine kill IS recorded."""

    proc = subprocess.Popen([sys.executable, "-c", _SLEEPER], start_new_session=True)  # noqa: S603
    try:
        _seed(app, proc.pid, pid_identity(proc.pid))  # identity matches → really killable

        outcome = await app.runs.cancel("run_xproc")

        assert outcome is CancelOutcome.TERMINATED
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and is_pid_alive(proc.pid):
            time.sleep(0.05)
        assert not is_pid_alive(proc.pid)
        run = app.runs.get("run_xproc")
        assert run.status == RunStatus.CANCELLED
        events = [
            json.loads(line)
            for line in (app.paths.run_dir("run_xproc") / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        assert events[-1]["type"] == "run.cancelled"
        assert (
            json.loads((app.paths.run_dir("run_xproc") / "status.json").read_text())["status"]
            == "cancelled"
        )
    finally:
        terminate_process_tree(proc.pid)
