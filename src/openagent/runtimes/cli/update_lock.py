"""Inspecting a foreign updater's lock, and clearing it only when it is provably dead (spec §21.8).

Antigravity's own updater takes a lock at ``~/.gemini/antigravity-cli/updater/update.lock``. When it
is held, OpenAgent refuses to update — correctly: two updaters rewriting one binary interleave into a
corrupt install that neither reports as failed.

The gap that leaves is a **stale** lock. An updater killed mid-run leaves the file behind, and from
then on every update is blocked with no way out except deleting a file the user has no reason to know
about. So the lock is inspected rather than merely tested for existence.

Three rules, because this is someone else's file:

* **Never remove it automatically.** Staleness is inferred, and an inference that deletes a lock a
  live process is holding causes exactly the corrupt install the lock exists to prevent.
* **Liveness beats age.** A process that is alive holds the lock however old the file is; a large
  age only matters when nothing owns it. A long-running update is not a stale one.
* **An unreadable lock is "unknown", not "stale".** A format this code does not recognise is a lock
  it cannot reason about, and guessing in the permissive direction is the dangerous guess.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: How long a lock with no identifiable live owner must sit before it is *considered* abandoned.
#: Generous: a slow update on a slow connection is a real thing, and being wrong here means
#: interrupting one.
STALE_AFTER = timedelta(hours=6)

#: Where Antigravity's updater keeps its lock.
ANTIGRAVITY_LOCK = Path.home() / ".gemini" / "antigravity-cli" / "updater" / "update.lock"

#: Cap on how much of a foreign lock file is read. It is not our format and it may not be small.
_MAX_LOCK_BYTES = 64 * 1024

_PID_PATTERN = re.compile(r'"?pid"?\s*[:=]\s*(\d+)')


class LockOwnerState:
    """What is known about whoever holds the lock."""

    ALIVE = "alive"
    GONE = "gone"
    UNKNOWN = "unknown"


@dataclass
class UpdateLockReport:
    """Everything Doctor shows about a foreign updater lock, and what may be done about it."""

    path: Path
    present: bool
    #: The pid recorded in the lock, when one could be read.
    pid: int | None = None
    #: Whether that pid is currently a live process.
    owner_state: str = LockOwnerState.UNKNOWN
    #: The owning process's create time, when it is alive. Recorded because a pid alone is not an
    #: identity — pids are reused, and a recycled pid would make a dead lock look held.
    owner_create_time: float | None = None
    #: The process's own name, for the operator to recognise. Never its command line, which can
    #: carry a token.
    owner_name: str | None = None
    age: timedelta | None = None
    #: Set when the file could not be read or understood.
    unreadable_reason: str | None = None

    @property
    def stale(self) -> bool:
        """Whether this lock is *provably* abandoned.

        Both conditions, never either: nothing owns it, **and** it has sat long enough that a
        just-started updater which has not yet written its pid is not being mistaken for a corpse.
        """

        if not self.present:
            return False
        if self.owner_state != LockOwnerState.GONE:
            return False
        return self.age is not None and self.age > STALE_AFTER

    @property
    def blocks_update(self) -> bool:
        return self.present

    def summary(self) -> str:
        if not self.present:
            return "no updater lock is present"
        parts = [f"an updater lock is held at {self.path}"]
        if self.pid is not None:
            if self.owner_state == LockOwnerState.ALIVE:
                who = f"pid {self.pid}"
                if self.owner_name:
                    who += f" ({self.owner_name})"
                parts.append(f"owned by a running process, {who}")
            elif self.owner_state == LockOwnerState.GONE:
                parts.append(f"its recorded owner (pid {self.pid}) is no longer running")
        elif self.unreadable_reason:
            parts.append(f"its owner could not be determined ({self.unreadable_reason})")
        if self.age is not None:
            hours = self.age.total_seconds() / 3600
            parts.append(f"{hours:.1f}h old")
        if self.stale:
            parts.append("it appears abandoned and can be cleared with your approval")
        return "; ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "present": self.present,
            "pid": self.pid,
            "owner_state": self.owner_state,
            "owner_name": self.owner_name,
            "age_seconds": int(self.age.total_seconds()) if self.age else None,
            "stale": self.stale,
            "blocks_update": self.blocks_update,
            "unreadable_reason": self.unreadable_reason,
        }


def inspect_update_lock(
    path: Path | None = None, *, now: datetime | None = None
) -> UpdateLockReport:
    """Read a foreign updater lock without touching it."""

    target = path or ANTIGRAVITY_LOCK
    report = UpdateLockReport(path=target, present=False)

    try:
        # lstat, not stat: a symlink where a lock file is expected is not something to follow.
        stat = target.lstat()
    except (OSError, ValueError):
        return report
    if not os.path.isfile(target) or os.path.islink(target):
        # A directory or a symlink in the lock's place is not a lock this code understands, and it
        # is emphatically not something to delete.
        report.present = target.exists()
        report.unreadable_reason = "the lock path is not a regular file"
        return report

    report.present = True
    moment = now or datetime.now(timezone.utc)
    report.age = moment - datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

    pid = _read_pid(target, report)
    if pid is None:
        return report
    report.pid = pid
    _resolve_owner(report)
    return report


def _read_pid(target: Path, report: UpdateLockReport) -> int | None:
    """Pull a pid out of the lock, tolerating a format that is not ours.

    JSON first, then a loose ``pid: N``, then a file that is just a number. Anything else leaves the
    owner unknown — which keeps the lock un-clearable, the safe direction.
    """

    try:
        raw = target.read_bytes()[:_MAX_LOCK_BYTES].decode("utf-8", "replace").strip()
    except OSError as exc:
        report.unreadable_reason = f"the lock could not be read ({exc.__class__.__name__})"
        return None
    if not raw:
        report.unreadable_reason = "the lock file is empty"
        return None

    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            value = payload.get("pid")
            if isinstance(value, int) and value > 0:
                return value
    except ValueError:
        pass

    match = _PID_PATTERN.search(raw)
    if match:
        return int(match.group(1))
    if raw.isdigit():
        return int(raw)

    report.unreadable_reason = "the lock does not record a process id in a recognised form"
    return None


def _resolve_owner(report: UpdateLockReport) -> None:
    """Decide whether the recorded pid is a live process.

    A pid alone is not an identity — pids are reused — so the process's create time and name are
    recorded alongside. Without psutil the answer is ``UNKNOWN``, which keeps the lock un-clearable
    rather than guessing from a bare ``kill(pid, 0)`` that cannot tell a reused pid from the original.
    """

    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a declared dependency
        report.owner_state = LockOwnerState.UNKNOWN
        report.unreadable_reason = "process inspection is unavailable"
        return

    try:
        process = psutil.Process(report.pid)
        report.owner_state = LockOwnerState.ALIVE
        report.owner_create_time = process.create_time()
        # The name, never the command line: an updater's argv can carry a token.
        report.owner_name = process.name()
    except psutil.NoSuchProcess:
        report.owner_state = LockOwnerState.GONE
    except (psutil.AccessDenied, psutil.Error, OSError):
        # A process we cannot inspect is still a process. Unknown, not gone.
        report.owner_state = LockOwnerState.UNKNOWN
        report.unreadable_reason = "the owning process could not be inspected"


class StaleLockError(RuntimeError):
    """A lock was asked to be cleared that is not provably abandoned."""


def clear_stale_lock(report: UpdateLockReport, *, approved: bool = False) -> bool:
    """Remove an abandoned updater lock. Requires the lock to be stale **and** explicit approval.

    Returns whether it was removed. Both gates are real:

    * ``approved`` is a parameter with no default-true path, because this deletes a file OpenAgent
      does not own, in another tool's directory;
    * staleness is re-derived from the report rather than trusted from a flag, so a caller cannot
      pass a hand-built report to force the removal of a live lock.
    """

    if not report.present:
        return False
    if not report.stale:
        raise StaleLockError(
            f"the updater lock at {report.path} is not provably abandoned "
            f"(owner: {report.owner_state}); OpenAgent will not remove a lock that may be held"
        )
    if not approved:
        raise StaleLockError(
            f"clearing the updater lock at {report.path} removes a file OpenAgent does not own "
            f"and needs explicit approval"
        )
    try:
        report.path.unlink()
    except FileNotFoundError:
        # Someone else cleaned it up between the inspection and now. The desired state is reached.
        return True
    except OSError as exc:
        raise StaleLockError(f"the lock could not be removed ({exc.__class__.__name__})") from exc
    return True
