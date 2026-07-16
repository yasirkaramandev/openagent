import os
import subprocess
import sys

import psutil
import pytest

from openagent.security.process import (
    PID_ALIVE,
    PID_GONE,
    PID_REUSED,
    PID_UNKNOWN,
    TerminationOutcome,
    capture_process_identity,
    is_pid_alive,
    minimal_environment,
    process_identity_status,
    run_process_status,
    terminate_pid_tree,
    terminate_process_tree,
)


def test_minimal_env_excludes_api_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))
    env = minimal_environment()
    assert "OPENAI_API_KEY" not in env
    assert "PATH" in env


def test_minimal_env_injects_extra():
    env = minimal_environment({"CODEX_API_KEY": "sk-run-scoped"})
    assert env["CODEX_API_KEY"] == "sk-run-scoped"


# --------------------------------------------------------------------------- PID identity (item 11)


@pytest.fixture()
def live_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        yield proc, psutil.Process(proc.pid).create_time()
    finally:
        proc.kill()
        proc.wait()


def test_pid_status_no_pid_is_gone():
    assert run_process_status(None, None) == PID_GONE


def test_pid_status_dead_pid_is_gone():
    assert run_process_status(2_000_000_000, 123.0) == PID_GONE


def test_pid_status_live_matching_create_time_is_alive(live_process):
    proc, created = live_process
    assert run_process_status(proc.pid, created) == PID_ALIVE


def test_pid_status_live_different_create_time_is_reused(live_process):
    proc, created = live_process
    # Same live PID, but the recorded start-time is off by an hour -> a different process.
    assert run_process_status(proc.pid, created - 3600.0) == PID_REUSED


def test_pid_status_live_without_recorded_time_is_unknown(live_process):
    proc, _ = live_process
    assert run_process_status(proc.pid, None) == PID_UNKNOWN


# --------------------------------------------------------------------------- identity fail-closed (§6)


def test_terminate_pid_tree_refuses_without_a_complete_identity():
    """A missing create-time is NOT a licence to kill (spec §6).

    Until v0.1.3 `terminate_pid_tree(pid, None)` skipped identity verification entirely: it checked
    only that the PID *existed* and then terminated it. PIDs are recycled, so any run whose
    create-time was never captured could kill a completely unrelated process that inherited its
    number. `run_process_status` already classified this as PID_UNKNOWN; the killer ignored it.
    """

    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        result = terminate_pid_tree(None)
        assert result.outcome is TerminationOutcome.IDENTITY_UNKNOWN
        assert is_pid_alive(proc.pid), (
            "the process was killed despite failing identity verification"
        )
    finally:
        identity = capture_process_identity(proc.pid)
        if identity is not None:
            terminate_process_tree(identity)


def test_terminate_pid_tree_refuses_a_reused_pid():
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        identity = capture_process_identity(proc.pid)
        assert identity is not None
        wrong = identity.model_copy(update={"create_time": identity.create_time - 10_000.0})
        result = terminate_pid_tree(wrong)
        assert result.outcome is TerminationOutcome.IDENTITY_MISMATCH
        assert is_pid_alive(proc.pid)
    finally:
        identity = capture_process_identity(proc.pid)
        if identity is not None:
            terminate_process_tree(identity)


def test_terminate_pid_tree_kills_a_verified_process():
    """The positive path still works: a matching identity really is terminated."""

    import time as _time

    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        identity = capture_process_identity(proc.pid)
        assert identity is not None
        assert process_identity_status(identity) == PID_ALIVE
        result = terminate_pid_tree(identity)
        assert result.outcome is TerminationOutcome.TERMINATED
        assert result.verified_terminated
        deadline = _time.monotonic() + 5.0
        while _time.monotonic() < deadline and is_pid_alive(proc.pid):
            _time.sleep(0.05)
        assert not is_pid_alive(proc.pid)
    finally:
        identity = capture_process_identity(proc.pid)
        if identity is not None:
            terminate_process_tree(identity)
