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
