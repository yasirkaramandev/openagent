"""The export lock must be a real cross-process lock (spec §6).

The old lock was an ``O_EXCL`` sentinel file plus the rule "delete it if it is more than 30 seconds
old". That rule cannot tell a crashed owner from a slow one, and it misfires in exactly the case it
was meant to handle: a large export on a slow disk is alive and holding the lock legitimately, and
after 30 seconds a second process deletes its lock file and writes the same path concurrently.

These tests use **real subprocesses**, not threads. A thread-based test would pass against a lock
that is only a Python-level mutex, which is precisely the class of bug being excluded here.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from openagent.security.file_lock import LockTimeout, file_lock

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"


def _spawn(body: str) -> subprocess.Popen[str]:
    preamble = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "from pathlib import Path\n"
        "from openagent.security.file_lock import file_lock, LockTimeout\n"
    )
    script = preamble + textwrap.dedent(body).strip() + "\n"
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_line(proc: subprocess.Popen[str], expected: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line.strip() == expected:
            return
        if not line and proc.poll() is not None:
            break
    raise AssertionError(f"child never reported {expected!r}")


def test_a_second_process_cannot_take_a_held_lock(tmp_path: Path) -> None:
    lock = tmp_path / "events.jsonl.lock"
    with file_lock(lock, timeout=5.0):
        child = _spawn(
            f"""
            try:
                with file_lock(Path({str(lock)!r}), timeout=0.5):
                    print("ACQUIRED", flush=True)
            except LockTimeout:
                print("TIMEOUT", flush=True)
            """
        )
        out, err = child.communicate(timeout=30)
        assert "TIMEOUT" in out, f"a second process took a held lock. stdout={out!r} stderr={err!r}"


def test_a_live_lock_is_not_stolen_because_the_file_looks_old(tmp_path: Path) -> None:
    """The exact regression: age is not evidence that the owner died.

    The lock file's mtime is pushed ten minutes into the past while a live holder still owns it.
    Under the old age-based rule the waiter would delete the file and proceed; under an OS lock it
    correctly keeps waiting, because the owner is alive.
    """

    lock = tmp_path / "events.jsonl.lock"
    with file_lock(lock, timeout=5.0):
        stale = time.time() - 600
        os.utime(lock, (stale, stale))

        child = _spawn(
            f"""
            from pathlib import Path
            try:
                with file_lock(Path({str(lock)!r}), timeout=0.5):
                    print("ACQUIRED", flush=True)
            except LockTimeout:
                print("TIMEOUT", flush=True)
            """
        )
        out, err = child.communicate(timeout=30)
        assert "TIMEOUT" in out, (
            f"an aged-out lock file was stolen from a live owner. stdout={out!r} stderr={err!r}"
        )
        assert lock.exists(), "the waiter deleted a live owner's lock file"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="SIGKILL semantics are POSIX-specific")
def test_the_os_releases_the_lock_when_the_owner_is_killed(tmp_path: Path) -> None:
    """No cleanup code runs on SIGKILL, so only the kernel can release it — and it must."""

    lock = tmp_path / "events.jsonl.lock"
    child = _spawn(
        f"""
        from pathlib import Path
        with file_lock(Path({str(lock)!r}), timeout=10):
            print("ACQUIRED", flush=True)
            time.sleep(300)
        """
    )
    try:
        _wait_for_line(child, "ACQUIRED")

        # While the child holds it, this process must not be able to take it.
        with pytest.raises(LockTimeout):
            with file_lock(lock, timeout=0.5):
                pass

        child.send_signal(signal.SIGKILL)
        child.wait(timeout=10)

        # The owner is gone; the kernel has dropped the lock and it is now available.
        deadline = time.monotonic() + 10
        while True:
            try:
                with file_lock(lock, timeout=0.5):
                    break
            except LockTimeout:
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        "the lock was never released after the owner was killed"
                    ) from None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_lock_is_reentrant_across_sequential_acquisitions(tmp_path: Path) -> None:
    """Releasing must actually release — a leaked descriptor would deadlock the next export."""

    lock = tmp_path / "events.jsonl.lock"
    for _ in range(5):
        with file_lock(lock, timeout=2.0):
            pass
    with file_lock(lock, timeout=2.0):
        pass


def test_timeout_is_reported_not_swallowed(tmp_path: Path) -> None:
    """A caller that did not get the lock must learn that, rather than proceed as if it had."""

    lock = tmp_path / "events.jsonl.lock"
    child = _spawn(
        f"""
        from pathlib import Path
        with file_lock(Path({str(lock)!r}), timeout=10):
            print("ACQUIRED", flush=True)
            time.sleep(30)
        """
    )
    try:
        _wait_for_line(child, "ACQUIRED")
        with pytest.raises(LockTimeout) as excinfo:
            with file_lock(lock, timeout=0.3):
                pass
        assert "timed out" in str(excinfo.value)
    finally:
        child.kill()
        child.wait(timeout=10)
