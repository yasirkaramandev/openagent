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
    minimal_environment,
    run_process_status,
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


def test_terminate_pid_tree_refuses_without_a_recorded_create_time():
    """A missing create-time is NOT a licence to kill (spec §6).

    Until v0.1.3 `terminate_pid_tree(pid, None)` skipped identity verification entirely: it checked
    only that the PID *existed* and then terminated it. PIDs are recycled, so any run whose
    create-time was never captured could kill a completely unrelated process that inherited its
    number. `run_process_status` already classified this as PID_UNKNOWN; the killer ignored it.
    """

    import subprocess
    import sys

    from openagent.security.process import (
        is_pid_alive,
        terminate_pid_tree,
        terminate_process_tree,
    )

    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        assert terminate_pid_tree(proc.pid, None) is False, (
            "an unverifiable PID must never be terminated"
        )
        assert is_pid_alive(proc.pid), (
            "the process was killed despite failing identity verification"
        )
    finally:
        terminate_process_tree(proc.pid)


def test_terminate_pid_tree_refuses_a_reused_pid():
    import subprocess
    import sys
    import time as _time

    from openagent.security.process import (
        is_pid_alive,
        pid_identity,
        terminate_pid_tree,
        terminate_process_tree,
    )

    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        wrong = (pid_identity(proc.pid) or _time.time()) - 10_000.0
        assert terminate_pid_tree(proc.pid, wrong) is False
        assert is_pid_alive(proc.pid)
    finally:
        terminate_process_tree(proc.pid)


def test_terminate_pid_tree_kills_a_verified_process():
    """The positive path still works: a matching identity really is terminated."""

    import subprocess
    import sys
    import time as _time

    from openagent.security.process import (
        is_pid_alive,
        pid_identity,
        terminate_pid_tree,
        terminate_process_tree,
    )

    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        assert terminate_pid_tree(proc.pid, pid_identity(proc.pid)) is True
        deadline = _time.monotonic() + 5.0
        while _time.monotonic() < deadline and is_pid_alive(proc.pid):
            _time.sleep(0.05)
        assert not is_pid_alive(proc.pid)
    finally:
        terminate_process_tree(proc.pid)
