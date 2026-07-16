"""Cancelling an orphaned, still-alive process actually terminates it (§3).

item 9.5 marks a run whose owning OpenAgent process has gone but whose backend process is *still
running* as ``orphaned`` / ``orphaned_unattached_process``, and tells the user to stop it with
``openagent cancel --id <run-id>``. Before v0.1.2 ``cancel()`` rejected every terminal status
(orphaned included) and did nothing — a broken promise. These tests reproduce the orphan with a
**real** subprocess tree (a parent that spawns a grandchild), simulate the restart with a fresh
``OpenAgentApp`` on the same DB, and prove the whole tree is terminated on cancel — while a
reused/unknown PID is never touched.
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
from openagent.security.process import (
    is_pid_alive,
    pid_identity,
    terminate_process_tree,
)
from openagent.services.run_service import CancelOutcome
from openagent.storage.event_log import EventLog

# A parent process that spawns a grandchild, records both PIDs to a marker file, then sleeps. Killing
# the *tree* must take out both — proving cancellation is not just a single-PID SIGTERM.
_PARENT = (
    "import os, subprocess, sys, time\n"
    "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()) + '\\n' + str(gc.pid) + '\\n')\n"
    "time.sleep(120)\n"
)


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


def _spawn_tree(tmp_path: Path) -> tuple[subprocess.Popen, int, int]:
    """Start the parent+grandchild tree; return (proc, parent_pid, grandchild_pid)."""

    marker = tmp_path / "pids.txt"
    proc = subprocess.Popen(  # noqa: S603 - trusted local test helper
        [sys.executable, "-c", _PARENT, str(marker)],
        start_new_session=True,
    )
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if marker.exists():
            parts = marker.read_text().split()
            if len(parts) == 2:
                return proc, int(parts[0]), int(parts[1])
        time.sleep(0.02)
    terminate_process_tree(proc.pid)
    raise AssertionError("child tree never reported its PIDs")


def _seed_orphan_run(app: OpenAgentApp, pid: int, create_time: float | None) -> Run:
    """Persist a RUNNING run + a realistic run_dir/events.jsonl, as a real run would have left it."""

    run = Run(
        id="run_orphan01",
        agent="ghost",
        status=RunStatus.RUNNING,
        pid=pid,
        pid_started_at=create_time,
    )
    app.repos.runs.upsert(run)
    run_dir = app.paths.run_dir(run.id)
    run_dir.mkdir(parents=True, exist_ok=True)
    log = EventLog(run_dir, index=app.repos.event_index)
    log.append(
        NormalizedEvent(
            run_id=run.id, type=EventType.RUN_STARTED, source="openagent", data={"agent": "ghost"}
        )
    )
    log.append(
        NormalizedEvent(
            run_id=run.id,
            type=EventType.PROCESS_STARTED,
            source="fake-cli",
            data={"pid": pid, "create_time": create_time},
        )
    )
    return run


async def test_orphaned_live_process_is_cancelled(app: OpenAgentApp, tmp_path: Path):
    proc, parent_pid, grandchild_pid = _spawn_tree(tmp_path)
    try:
        create_time = pid_identity(parent_pid)
        _seed_orphan_run(app, parent_pid, create_time)

        # Fresh app == a restart: it owns no adapters/cancellations for this run.
        restarted = OpenAgentApp(app.paths)
        recovered = restarted.runs.recover_orphans()
        assert "run_orphan01" in recovered
        orphan = restarted.runs.get("run_orphan01")
        assert orphan.status == RunStatus.ORPHANED
        assert orphan.failure_type == "orphaned_unattached_process"
        # The process (and its grandchild) are genuinely still alive at this point.
        assert is_pid_alive(parent_pid) and is_pid_alive(grandchild_pid)

        outcome = await restarted.runs.cancel("run_orphan01")
        assert outcome is CancelOutcome.TERMINATED

        # The whole tree is gone — parent and grandchild.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and (
            is_pid_alive(parent_pid) or is_pid_alive(grandchild_pid)
        ):
            time.sleep(0.05)
        assert not is_pid_alive(parent_pid), "orphaned parent still alive after cancel"
        assert not is_pid_alive(grandchild_pid), "orphaned grandchild still alive after cancel"

        # DB is authoritative: cancelled, with the user-terminated failure type.
        final = restarted.runs.get("run_orphan01")
        assert final.status == RunStatus.CANCELLED
        assert final.failure_type == "orphaned_process_terminated_by_user"
        assert json.loads(restarted.runs.output("run_orphan01", "status"))["status"] == "cancelled"

        # run.cancelled is the LAST log entry; the audit note recording the kill precedes it.
        events = [
            json.loads(line)
            for line in restarted.runs.output("run_orphan01", "events").splitlines()
            if line.strip()
        ]
        assert events[-1]["type"] == "run.cancelled"
        assert any(e["type"] == "log" and e["data"].get("killed") is True for e in events)
    finally:
        terminate_process_tree(proc.pid)


async def test_orphaned_reused_pid_is_never_killed(app: OpenAgentApp, tmp_path: Path):
    """A live PID whose create-time does NOT match ours must never be terminated (§3.4 step 10)."""

    proc, parent_pid, grandchild_pid = _spawn_tree(tmp_path)
    try:
        # Record a *wrong* create-time so identity resolves to PID_REUSED, not PID_ALIVE.
        wrong_time = (pid_identity(parent_pid) or time.time()) - 10_000.0
        _seed_orphan_run(app, parent_pid, wrong_time)

        restarted = OpenAgentApp(app.paths)
        restarted.runs.recover_orphans()
        orphan = restarted.runs.get("run_orphan01")
        assert orphan.status == RunStatus.ORPHANED
        # A reused PID is classified as such and is NOT the terminable orphan reason.
        assert orphan.failure_type == "orphaned_pid_reused"

        outcome = await restarted.runs.cancel("run_orphan01")
        assert outcome is CancelOutcome.NOT_CANCELLABLE
        # The unrelated live process is untouched.
        assert is_pid_alive(parent_pid) and is_pid_alive(grandchild_pid)
        # And the run was not falsely flipped to cancelled.
        assert restarted.runs.get("run_orphan01").status == RunStatus.ORPHANED
    finally:
        terminate_process_tree(proc.pid)


async def test_orphaned_unknown_pid_is_never_killed(app: OpenAgentApp, tmp_path: Path):
    """A live PID with no recorded create-time is unverifiable → never killed (fail closed)."""

    proc, parent_pid, grandchild_pid = _spawn_tree(tmp_path)
    try:
        _seed_orphan_run(app, parent_pid, None)  # no create-time recorded → PID_UNKNOWN

        restarted = OpenAgentApp(app.paths)
        restarted.runs.recover_orphans()
        orphan = restarted.runs.get("run_orphan01")
        assert orphan.status == RunStatus.ORPHANED
        assert orphan.failure_type == "orphaned_pid_unknown"

        outcome = await restarted.runs.cancel("run_orphan01")
        assert outcome is CancelOutcome.NOT_CANCELLABLE
        assert is_pid_alive(parent_pid) and is_pid_alive(grandchild_pid)
    finally:
        terminate_process_tree(proc.pid)


async def test_orphan_unattached_but_pid_reused_at_cancel_time(app: OpenAgentApp, tmp_path: Path):
    """Even the *right* orphan reason re-verifies identity at cancel time and refuses a mismatch.

    The run was recovered as ``orphaned_unattached_process``, but by the time the user cancels, the
    PID no longer matches our recorded create-time (process exited / PID reused). We must not kill it.
    """

    proc, parent_pid, grandchild_pid = _spawn_tree(tmp_path)
    try:
        run = _seed_orphan_run(app, parent_pid, pid_identity(parent_pid))
        # Force the terminable reason, then corrupt the recorded identity so the cancel-time check
        # sees a mismatch.
        run.status = RunStatus.ORPHANED
        run.failure_type = "orphaned_unattached_process"
        run.pid_started_at = (run.pid_started_at or time.time()) - 10_000.0
        app.repos.runs.upsert(run)

        outcome = await app.runs.cancel("run_orphan01")
        assert outcome is CancelOutcome.IDENTITY_MISMATCH
        assert is_pid_alive(parent_pid) and is_pid_alive(grandchild_pid)
        assert app.runs.get("run_orphan01").status == RunStatus.ORPHANED
    finally:
        terminate_process_tree(proc.pid)


async def test_cancel_unknown_run_reports_not_found(app: OpenAgentApp):
    assert await app.runs.cancel("run_does_not_exist") is CancelOutcome.NOT_FOUND


async def test_cancel_completed_run_reports_already_terminal(app: OpenAgentApp):
    run = Run(id="run_done", agent="ghost", status=RunStatus.COMPLETED)
    app.repos.runs.upsert(run)
    assert await app.runs.cancel("run_done") is CancelOutcome.ALREADY_TERMINAL
