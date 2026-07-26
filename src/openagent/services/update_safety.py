"""Fail-closed preconditions for replacing the running OpenAgent installation (spec §3.1-§3.2).

Two questions have to be answered before the updater is allowed to overwrite the active binary,
and both were previously answered optimistically:

* **Is anything using this installation right now?** The old probe returned "no other processes"
  whenever it could not look — an ImportError, a permission error, an unreadable process table all
  collapsed into the same empty list that a genuinely quiet machine produces. And ``--force``
  bypassed a *positive* result outright, so "OpenAgent is mid-run" and "OpenAgent is idle in
  another tab" were treated identically.

* **Is the target commit actually forward of what is installed?** Versions were compared as
  strings. Within a release candidate every commit reports the same version, so "same version,
  different commit" was classified as a refresh and installed unconditionally — including commits
  *behind* the running one, or from an unrelated history.

Both are answered here with an explicit state that includes "I could not tell", and in both cases
that state blocks. The asymmetry worth stating: ``--force`` and ``--allow-downgrade`` are overrides
for things the operator can see and accept (a second idle window, a deliberate rollback). Neither
is an override for ignorance.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- process safety


class ProcessSafetyState(str, Enum):
    """What the machine is doing with this installation, including "unable to tell"."""

    SAFE = "safe"
    ACTIVE_IDLE_PROCESS = "active_idle_process"
    ACTIVE_RUN = "active_run"
    ACTIVE_TUI = "active_tui"
    UNKNOWN = "unknown"


class ProcessSafety(BaseModel):
    state: ProcessSafetyState
    identities: list[str] = Field(default_factory=list)
    detail: str = ""


#: States where ``--force`` is a legitimate operator override. An idle second process is a nuisance;
#: an active run or an open TUI is data being written, and an UNKNOWN probe is an unanswered
#: question. Neither of the latter is something ``--force`` gets to assert away.
_FORCEABLE = frozenset({ProcessSafetyState.SAFE, ProcessSafetyState.ACTIVE_IDLE_PROCESS})


def update_permitted(safety: ProcessSafety, *, force: bool) -> bool:
    if safety.state is ProcessSafetyState.SAFE:
        return True
    return force and safety.state in _FORCEABLE


#: Ordered most-dangerous-first, so a mixed process table reports the worst thing it found.
_PRECEDENCE = (
    ProcessSafetyState.UNKNOWN,
    ProcessSafetyState.ACTIVE_RUN,
    ProcessSafetyState.ACTIVE_TUI,
    ProcessSafetyState.ACTIVE_IDLE_PROCESS,
)

#: Package-manager and updater command lines that legitimately mention "openagent" while *being*
#: the update. Matching these as live OpenAgent processes would make the updater block on itself.
_UPDATER_TOKENS = ("uv tool", "pip install", "self-update", "openagent update")

_TUI_TOKENS = ("tui", "openagent-tui")
_RUN_TOKENS = ("run start", "run resume", "runs start", "run ", "agent run")


def _classify_command(haystack: str) -> ProcessSafetyState:
    if any(token in haystack for token in _TUI_TOKENS):
        return ProcessSafetyState.ACTIVE_TUI
    if any(token in haystack for token in _RUN_TOKENS):
        return ProcessSafetyState.ACTIVE_RUN
    return ProcessSafetyState.ACTIVE_IDLE_PROCESS


def _attr(proc: Any, key: str) -> tuple[bool, Any]:
    """Read one process attribute. Returns ``(readable, value)`` — never raises.

    ``readable`` is False when the attribute could not be read at all (AccessDenied, a process that
    exited mid-scan, a platform that does not expose it). The caller decides whether that ignorance
    is survivable for the process in question.
    """

    try:
        info = getattr(proc, "info", None)
        if isinstance(info, dict) and key in info:
            return True, info[key]
    except Exception:
        return False, None
    try:
        value = getattr(proc, key)
        return True, value() if callable(value) else value
    except Exception:
        return False, None
    return False, None


def probe_process_safety(
    *,
    process_lister: Callable[[], Iterable[Any]] | None = None,
    run_probe: Callable[[], Sequence[str]] | None = None,
    pid: int | None = None,
    uid: int | None = None,
    ancestors: set[int] | None = None,
    psutil_ok: bool = True,
) -> ProcessSafety:
    """Classify what is currently using this installation, failing closed on any blind spot.

    Every early return that reports something other than SAFE is a case the old probe reported as
    an empty list.
    """

    me = os.getpid() if pid is None else pid
    my_uid = os.getuid() if uid is None and hasattr(os, "getuid") else uid

    # 1) Database-tracked runs are authoritative: a leased run is active even if its process is not
    #    recognisable by command line (a detached backend, a differently-named entrypoint).
    try:
        active_runs = list((run_probe or active_run_identities)())
    except Exception as exc:
        return ProcessSafety(
            state=ProcessSafetyState.UNKNOWN,
            detail=f"active runs could not be determined ({exc.__class__.__name__})",
        )
    if active_runs:
        return ProcessSafety(
            state=ProcessSafetyState.ACTIVE_RUN,
            identities=[str(item) for item in active_runs[:5]],
            detail="the database reports an active run",
        )

    if not psutil_ok:
        return ProcessSafety(
            state=ProcessSafetyState.UNKNOWN,
            detail="psutil is unavailable, so running processes could not be enumerated",
        )

    if process_lister is None:
        try:
            import psutil
        except Exception:
            return ProcessSafety(
                state=ProcessSafetyState.UNKNOWN,
                detail="psutil is unavailable, so running processes could not be enumerated",
            )

        def process_lister() -> Iterable[Any]:
            return psutil.process_iter(["pid", "name", "cmdline", "uids", "create_time", "exe"])

    if ancestors is None:
        ancestors = _own_process_tree(me)
        if ancestors is None:
            return ProcessSafety(
                state=ProcessSafetyState.UNKNOWN,
                detail="this process's own identity could not be established",
            )

    try:
        processes = list(process_lister())
    except Exception as exc:
        return ProcessSafety(
            state=ProcessSafetyState.UNKNOWN,
            detail=f"the process table could not be read ({exc.__class__.__name__})",
        )

    findings: dict[ProcessSafetyState, list[str]] = {}
    reasons: dict[ProcessSafetyState, str] = {}

    def record(state: ProcessSafetyState, identity: str, reason: str) -> None:
        findings.setdefault(state, []).append(identity)
        reasons.setdefault(state, reason)

    for proc in processes:
        pid_ok, proc_pid = _attr(proc, "pid")
        if not pid_ok or not isinstance(proc_pid, int):
            record(ProcessSafetyState.UNKNOWN, "pid ?", "a process had no readable pid")
            continue
        if proc_pid in ancestors:
            continue

        uid_ok, uids = _attr(proc, "uids")
        proc_uid = getattr(uids, "real", None) if uid_ok else None

        name_ok, name = _attr(proc, "name")
        cmd_ok, cmdline = _attr(proc, "cmdline")
        name_text = (name or "").lower() if name_ok else ""
        cmd_text = " ".join(cmdline).lower() if cmd_ok and cmdline else ""

        # A process we cannot read at all is only our problem if it could be *ours*. Another user's
        # processes cannot touch this user's install root, tool dir, or database.
        if not name_text and not cmd_text:
            if my_uid is not None and proc_uid is not None and proc_uid != my_uid:
                continue
            record(
                ProcessSafetyState.UNKNOWN,
                f"pid {proc_pid}",
                "a process owned by this user could not be inspected",
            )
            continue

        haystack = f"{name_text} {cmd_text}".strip()
        if "openagent" not in haystack:
            continue
        if any(token in haystack for token in _UPDATER_TOKENS):
            continue  # the update itself, or the package manager performing it

        # This is a candidate. From here on, anything we cannot verify blocks: a pid alone is not an
        # identity (pids are reused), and a process whose executable we cannot resolve cannot be
        # confirmed to be — or not be — this installation.
        create_ok, create_time = _attr(proc, "create_time")
        if not create_ok or create_time is None:
            record(
                ProcessSafetyState.UNKNOWN,
                f"pid {proc_pid}",
                "an OpenAgent process could not be identity-checked (no creation time; "
                "a bare pid may have been reused)",
            )
            continue
        exe_ok, exe = _attr(proc, "exe")
        if not exe_ok or not exe:
            record(
                ProcessSafetyState.UNKNOWN,
                f"pid {proc_pid}",
                "an OpenAgent process's executable could not be verified",
            )
            continue

        record(_classify_command(haystack), f"pid {proc_pid}", "another OpenAgent process is live")

    for state in _PRECEDENCE:
        if state in findings:
            return ProcessSafety(state=state, identities=findings[state][:5], detail=reasons[state])
    return ProcessSafety(state=ProcessSafetyState.SAFE, detail="no other OpenAgent process is live")


def active_run_identities(db_path: Any = None) -> list[str]:
    """Runs that are mid-flight according to the database.

    Read with sqlite3 directly rather than through the ORM stack, because ``openagent update`` has
    to keep working as a repair path when the application layer cannot start — a schema this binary
    is too old to map must not turn into "no active runs".

    A missing database means a fresh install with nothing running, and returns empty. A database
    that *exists* but cannot be read raises, which the caller turns into UNKNOWN: an unreadable run
    table is a blind spot, not an all-clear.
    """

    import sqlite3
    from pathlib import Path

    if db_path is None:
        from ..config import get_paths

        db_path = get_paths().db_path
    path = Path(db_path)
    if not path.exists():
        return []

    # Non-terminal statuses, plus any row still holding a turn lease. The lease matters
    # independently: a crashed owner leaves a lease behind, and its pid/create_time pair is the only
    # reliable way to tell a live writer from a dead one.
    live_statuses = ("queued", "starting", "running", "waiting_approval")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    try:
        rows = connection.execute(
            "SELECT id, status, active_turn_id, turn_owner_pid, turn_owner_create_time "
            "FROM runs WHERE status IN (?, ?, ?, ?) OR active_turn_id IS NOT NULL",
            live_statuses,
        ).fetchall()
    finally:
        connection.close()

    active: list[str] = []
    for run_id, status, turn_id, owner_pid, owner_create_time in rows:
        if turn_id is not None and not _lease_owner_alive(owner_pid, owner_create_time):
            # A lease whose owner is provably gone is a crashed run, not a live writer. It still
            # needs recovery, but it is not a reason to refuse an update.
            if str(status) not in live_statuses:
                continue
        active.append(f"{run_id} ({status})")
    return active


def _lease_owner_alive(pid: Any, create_time: Any) -> bool:
    """Whether a turn lease's owning process is still the same live process.

    The pid alone is not enough — pids are reused, and a reused pid would make a dead run look
    live forever. An unreadable answer counts as alive, so uncertainty never *clears* a lease.
    """

    if not pid:
        return False
    try:
        import psutil

        process = psutil.Process(int(pid))
        if create_time is None:
            return True
        return abs(process.create_time() - float(create_time)) < 1.0
    except Exception:
        try:
            import psutil

            return bool(psutil.pid_exists(int(pid)))
        except Exception:
            return True


def _own_process_tree(me: int) -> set[int] | None:
    """This process and its ancestors, or ``None`` if our own identity is unreadable."""

    try:
        import psutil

        tree = {me}
        for parent in psutil.Process(me).parents():
            tree.add(parent.pid)
        return tree
    except Exception:
        return None


def describe_process_block(safety: ProcessSafety, *, force: bool) -> str:
    """The operator-facing reason an update was refused, and what would unblock it."""

    identities = f" ({', '.join(safety.identities)})" if safety.identities else ""
    if safety.state is ProcessSafetyState.UNKNOWN:
        return (
            f"OpenAgent could not confirm that nothing else is using this installation: "
            f"{safety.detail}{identities}. Refusing to replace the running binary — this is not "
            f"overridable with --force, because the probe reported ignorance, not safety."
        )
    if safety.state is ProcessSafetyState.ACTIVE_RUN:
        return (
            f"an OpenAgent run is currently active{identities}. Finish or cancel it before "
            f"updating — --force cannot bypass an active run, because replacing the binary "
            f"underneath it can corrupt run state."
        )
    if safety.state is ProcessSafetyState.ACTIVE_TUI:
        return (
            f"the OpenAgent TUI is open{identities}. Close it before updating — --force cannot "
            f"bypass an open TUI."
        )
    if force:  # pragma: no cover - defensive; an idle process is forceable
        return f"another OpenAgent process is running{identities}"
    return (
        f"OpenAgent is currently running in another process{identities}. Close it before "
        f"updating, or pass --force."
    )


# ---------------------------------------------------------------- commit ancestry


class CommitRelation(str, Enum):
    """Where the target commit sits relative to the installed one, on the commit graph."""

    SAME = "same"
    AHEAD = "ahead"
    BEHIND = "behind"
    DIVERGED = "diverged"
    UNKNOWN = "unknown"


#: ``(base, head)`` -> ``"identical" | "ahead" | "behind" | "diverged" | None``. ``None``, or any
#: exception, means the question was not answered — which is not the same as "no difference".
AncestryOracle = Callable[[str, str], "str | None"]

_STATUS_TO_RELATION = {
    "identical": CommitRelation.SAME,
    "ahead": CommitRelation.AHEAD,
    "behind": CommitRelation.BEHIND,
    "diverged": CommitRelation.DIVERGED,
}


def classify_commit_relation(
    *, installed: str | None, target: str, compare: AncestryOracle
) -> CommitRelation:
    """Order two commits by ancestry rather than by version string.

    ``installed`` should be the *baseline* — the newest commit this installation has accepted, not
    merely the one currently unpacked, so a rollback cannot walk the install backwards one accepted
    commit at a time.
    """

    if not installed:
        return CommitRelation.UNKNOWN
    if installed == target:
        return CommitRelation.SAME
    try:
        status = compare(installed, target)
    except Exception:
        return CommitRelation.UNKNOWN
    if not isinstance(status, str):
        return CommitRelation.UNKNOWN
    return _STATUS_TO_RELATION.get(status.strip().lower(), CommitRelation.UNKNOWN)


def relation_permits_install(relation: CommitRelation, *, allow_downgrade: bool) -> bool:
    """Whether an install may proceed. UNKNOWN is never unlocked by ``--allow-downgrade``.

    Going backwards deliberately is a supported operation; installing a commit whose relationship to
    the running one could not be established is not, because "unknown" is exactly what a rollback
    attack looks like from here.
    """

    if relation in {CommitRelation.SAME, CommitRelation.AHEAD}:
        return True
    if relation is CommitRelation.UNKNOWN:
        return False
    return allow_downgrade


def describe_relation_block(relation: CommitRelation, *, installed: str | None, target: str) -> str:
    short_target = target[:12]
    short_installed = (installed or "unknown")[:12]
    if relation is CommitRelation.BEHIND:
        return (
            f"target commit {short_target} is an ancestor of the installed {short_installed} — "
            f"installing it would move this installation backwards. Re-run with "
            f"--allow-downgrade if that is intended."
        )
    if relation is CommitRelation.DIVERGED:
        return (
            f"target commit {short_target} is not a descendant of the installed "
            f"{short_installed}; the two histories have diverged. Re-run with --allow-downgrade "
            f"if that is intended."
        )
    return (
        f"the relationship between the installed commit {short_installed} and the target "
        f"{short_target} could not be established, so the update was refused. This is not "
        f"overridable with --allow-downgrade."
    )
