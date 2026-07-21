"""Race-safe async subprocess identity capture (spec §5).

The bug: a valid, fast child could exit between ``create_subprocess_exec`` returning and the first
``psutil`` sample. The old ``start`` yielded the loop exactly once, saw no return code yet, and then
terminated the (already finished) child and raised ``RuntimeError`` — a real success reported as a
startup failure. The synchronous sampler also ran ``time.sleep`` on the event-loop thread, stalling
every other run for the duration of the startup window.

These tests pin the contract of the new bounded async handshake: a child that completes before its
identity is pinned is fine (``identity is None``, output preserved); a child still alive at the
deadline with no readable identity is terminated fail-closed and raises a typed error; and identity
sampling never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from openagent.core.models import ProcessIdentity
from openagent.security import process as process_module
from openagent.security.process import (
    ManagedProcess,
    ProcessIdentityCaptureError,
    is_pid_alive,
    minimal_environment,
)

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="POSIX process-group semantics"
)


def _mp(tmp_path: Path, code: str) -> ManagedProcess:
    return ManagedProcess([sys.executable, "-c", code], cwd=tmp_path, env=minimal_environment())


def _identity(pid: int, executable: str) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid, create_time=123.0, executable=executable, command_identity="cmd"
    )


async def test_fast_successful_process_may_exit_before_identity_capture(
    tmp_path, monkeypatch
) -> None:
    """A child that finishes before its identity is pinned is a success, not a startup failure."""

    # The sampler never succeeds, and the timeout is generous, so the *only* way this returns is the
    # child's own exit winning the race — exactly the case that used to raise.
    monkeypatch.setattr(process_module, "_capture_process_identity_once", lambda _pid: None)
    monkeypatch.setattr(process_module, "_STARTUP_IDENTITY_TIMEOUT", 5.0)

    proc = _mp(tmp_path, "print('hi')")
    await proc.start()  # must not raise

    assert proc.identity is None
    assert await proc.wait() == 0


async def test_fast_process_stdout_is_preserved_without_identity(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(process_module, "_capture_process_identity_once", lambda _pid: None)
    monkeypatch.setattr(process_module, "_STARTUP_IDENTITY_TIMEOUT", 5.0)

    proc = _mp(tmp_path, "print('line-one'); print('line-two')")
    await proc.start()
    lines = [line async for line in proc.stream_stdout()]
    code = await proc.wait()

    assert code == 0
    assert "line-one" in lines and "line-two" in lines


async def test_identity_capture_retries_transient_none(tmp_path, monkeypatch) -> None:
    """A single transient ``None`` from psutil must not fail a live process."""

    real = process_module._capture_process_identity_once
    calls = 0

    def transient(pid: int) -> ProcessIdentity | None:
        nonlocal calls
        calls += 1
        return None if calls == 1 else real(pid)

    monkeypatch.setattr(process_module, "_capture_process_identity_once", transient)

    proc = _mp(tmp_path, "import time; time.sleep(1)")
    await proc.start()

    assert calls >= 2
    assert proc.identity is not None
    await proc.cancel()
    await proc.wait()


async def test_live_process_with_unavailable_identity_is_terminated(tmp_path, monkeypatch) -> None:
    """A still-live child whose identity never resolves is killed and raises — never left running."""

    monkeypatch.setattr(process_module, "_capture_process_identity_once", lambda _pid: None)
    monkeypatch.setattr(process_module, "_STARTUP_IDENTITY_TIMEOUT", 0.2)

    proc = _mp(tmp_path, "import time; time.sleep(30)")
    with pytest.raises(ProcessIdentityCaptureError):
        await proc.start()

    pid = proc.pid
    assert pid is not None
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and is_pid_alive(pid):
        await asyncio.sleep(0.02)
    assert not is_pid_alive(pid), "the unidentified live process was left running"


async def test_identity_capture_does_not_block_event_loop(tmp_path, monkeypatch) -> None:
    """Synchronous psutil sampling runs off the loop, so other coroutines keep making progress."""

    real = process_module._capture_process_identity_once

    def slow(pid: int) -> ProcessIdentity | None:
        time.sleep(0.03)  # if this ran on the loop thread, every await below would stall
        return real(pid)

    monkeypatch.setattr(process_module, "_capture_process_identity_once", slow)

    proc = _mp(tmp_path, "import time; time.sleep(2)")
    ticks = 0
    stop = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.005)
            ticks += 1

    hb = asyncio.create_task(heartbeat())
    await proc.start()
    stop.set()
    await hb

    assert proc.identity is not None
    # The startup window spans several blocking samples; a blocked loop would tick ~0 times.
    assert ticks >= 5, f"event loop appears blocked during identity capture (ticks={ticks})"
    await proc.cancel()
    await proc.wait()


async def test_reexec_process_returns_stable_post_exec_identity(tmp_path, monkeypatch) -> None:
    """A launcher that re-execs must yield the stable post-exec identity, not the transient one."""

    proc = _mp(tmp_path, "import time; time.sleep(2)")
    seen = 0

    def reexec(pid: int) -> ProcessIdentity:
        nonlocal seen
        seen += 1
        # First sample is the launcher; every later sample is the settled post-exec identity.
        return _identity(pid, "/launcher") if seen == 1 else _identity(pid, "/post-exec")

    monkeypatch.setattr(process_module, "_capture_process_identity_once", reexec)

    try:
        await proc.start()
        assert proc.identity is not None
        assert proc.identity.executable == "/post-exec"
    finally:
        pid = proc.pid
        if pid is not None and is_pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
        await proc.wait()


async def test_completed_process_is_not_mistaken_for_reused_pid(tmp_path, monkeypatch) -> None:
    """Once the child's own wait resolves, a different process at the reused PID is never adopted."""

    def reused(pid: int) -> ProcessIdentity:
        time.sleep(0.1)  # let the fast child finish first, so its PID is free to be "reused"
        return _identity(pid, "/somebody-else")

    monkeypatch.setattr(process_module, "_capture_process_identity_once", reused)
    monkeypatch.setattr(process_module, "_STARTUP_IDENTITY_TIMEOUT", 5.0)

    proc = _mp(tmp_path, "print('done')")
    await proc.start()

    assert proc.identity is None, "adopted an identity that belongs to a reused PID"
    lines = [line async for line in proc.stream_stdout()]
    assert any("done" in line for line in lines)
    assert await proc.wait() == 0


async def test_cancel_during_identity_capture_still_terminates(tmp_path, monkeypatch) -> None:
    """A cancel that arrives while identity is still being captured must still kill the process.

    Regression: async capture yields the loop, so ``pid`` becomes observable before ``identity`` is
    set. A cancel racing that window used to see ``identity is None`` and refuse to terminate, so the
    process ran to completion and the run finalized FAILED instead of CANCELLED.
    """

    real = process_module._capture_process_identity_once

    def slow(pid: int) -> ProcessIdentity | None:
        time.sleep(0.05)  # widen the pid-live-but-identity-unset window
        return real(pid)

    monkeypatch.setattr(process_module, "_capture_process_identity_once", slow)

    proc = _mp(tmp_path, "import time; print('up', flush=True); time.sleep(120)")
    start_task = asyncio.create_task(proc.start())
    # Mirror how a canceller finds the process: it becomes visible by pid before identity is set.
    while not (proc.pid and is_pid_alive(proc.pid)):
        await asyncio.sleep(0.002)
    identity_was_unset = proc.identity is None

    result = await proc.cancel()
    await start_task

    assert identity_was_unset, "test did not exercise the mid-capture window"
    assert result.verified_terminated
    deadline = time.monotonic() + 5.0
    pid = proc.pid
    while time.monotonic() < deadline and is_pid_alive(pid):
        await asyncio.sleep(0.02)
    assert not is_pid_alive(pid)


@pytest.mark.slow
async def test_many_fast_processes_start_without_false_identity_failure(tmp_path) -> None:
    """Stress the exact race: many short-lived children, no spurious startup failure, output kept."""

    semaphore = asyncio.Semaphore(20)

    async def one(index: int) -> tuple[int, bool]:
        async with semaphore:
            proc = _mp(tmp_path, f"print('ok-{index}')")
            await proc.start()
            lines = [line async for line in proc.stream_stdout()]
            code = await proc.wait()
            return code, any(f"ok-{index}" in line for line in lines)

    results = await asyncio.gather(*[one(i) for i in range(200)])
    assert all(code == 0 for code, _ in results)
    assert all(saw for _, saw in results)
