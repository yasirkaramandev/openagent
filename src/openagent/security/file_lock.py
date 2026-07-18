"""A cross-process file lock backed by the OS (spec §6).

The previous export lock was an ``O_EXCL`` sentinel file plus a rule: "if the file is more than 30
seconds old, assume the owner died and delete it". That rule is wrong in the one case that matters.
A large export on a slow disk is *alive* and holding the lock legitimately; after 30 seconds a second
process deletes its lock file, takes the lock, and both write the same path at once. The heuristic
fires precisely when contention is real.

Age cannot distinguish "crashed" from "still working", so this module stops guessing and asks the
kernel. ``flock`` (POSIX) and ``msvcrt.locking`` (Windows) tie ownership to an **open file
descriptor**: the lock is released when the owner closes it or when the process dies, by the OS, with
no timer and no cleanup rule to get wrong.

Two consequences worth stating plainly:

* the lock file is never unlinked. It is a zero-byte rendezvous point, and deleting it is what
  created the bug — a process can hold a lock on a file another process has already replaced;
* a timeout is reported as a timeout. The caller learns it did not get the lock, rather than
  proceeding as though it had.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_IS_WINDOWS = sys.platform.startswith("win")

if _IS_WINDOWS:  # pragma: no cover - platform-specific
    import msvcrt
else:
    import fcntl


class LockTimeout(TimeoutError):
    """The lock was held by a live owner for longer than the caller was willing to wait."""


def _try_acquire(fd: int) -> bool:
    """One non-blocking attempt. True on success, False when someone else holds it."""

    try:
        if _IS_WINDOWS:  # pragma: no cover - platform-specific
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _release(fd: int) -> None:
    with contextlib.suppress(OSError):
        if _IS_WINDOWS:  # pragma: no cover - platform-specific
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def file_lock(path: Path, *, timeout: float = 30.0, poll: float = 0.01) -> Iterator[None]:
    """Hold an exclusive OS-level lock on ``path`` for the duration of the block.

    Raises :class:`LockTimeout` if the lock cannot be taken within ``timeout``. It never breaks
    another holder's lock: a live owner keeps it however long its work takes, and a dead owner's lock
    is already gone because the kernel dropped it when the process exited.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while not _try_acquire(fd):
            if time.monotonic() >= deadline:
                raise LockTimeout(f"timed out after {timeout}s waiting for the lock at {path}")
            time.sleep(poll)
        # Record the owner for diagnostics only. Nothing reads this to make a decision — that was
        # the old design's mistake.
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        try:
            yield
        finally:
            _release(fd)
    finally:
        os.close(fd)
