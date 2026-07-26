"""Fail-closed active-process safety for the self-updater (spec §3.1).

The rule these tests pin down: ``--force`` is an override for *inconvenience*, not for *danger*.
It may proceed past an idle second process, because nothing is mid-write. It may never proceed
past an active run or an open TUI, and it may never proceed on a probe that could not run — an
enumeration that failed tells us nothing, and "nothing" is not "safe".
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from openagent.runtimes.cli.locator import CommandResult
from openagent.security.file_lock import file_lock
from openagent.services.self_update import (
    OFFICIAL_HTTPS_REMOTE,
    OpenAgentUpdateChannel,
    SelfUpdatePlan,
    perform_self_update,
)
from openagent.services.update_safety import (
    ProcessSafety,
    ProcessSafetyState,
    probe_process_safety,
    update_permitted,
)

A = "a" * 40
B = "b" * 40


# ------------------------------------------------------------------ decision matrix


@pytest.mark.parametrize(
    ("state", "normal_ok", "force_ok"),
    [
        (ProcessSafetyState.SAFE, True, True),
        (ProcessSafetyState.ACTIVE_IDLE_PROCESS, False, True),
        (ProcessSafetyState.ACTIVE_RUN, False, False),
        (ProcessSafetyState.ACTIVE_TUI, False, False),
        (ProcessSafetyState.UNKNOWN, False, False),
    ],
)
def test_decision_matrix(state, normal_ok, force_ok) -> None:
    safety = ProcessSafety(state=state, detail="fixture")
    assert update_permitted(safety, force=False) is normal_ok
    assert update_permitted(safety, force=True) is force_ok


# ------------------------------------------------------------------ probe classification


_DEFAULT = object()  # "not overridden", so that an explicit None means "unreadable"


class _Proc:
    """Minimal psutil.Process stand-in. ``raises`` simulates AccessDenied on attribute reads."""

    def __init__(
        self,
        pid: int,
        *,
        name: str = "openagent",
        cmdline=_DEFAULT,
        uid: int | None = None,
        create_time: float | None = 1000.0,
        exe: str | None = "/Users/x/.local/bin/openagent",
        raises: bool = False,
    ) -> None:
        self._raises = raises
        self.info = {
            "pid": pid,
            "name": name,
            "cmdline": ["openagent"] if cmdline is _DEFAULT else cmdline,
            "uids": None if uid is None else _Uids(uid),
            "create_time": create_time,
            "exe": exe,
        }

    def __getattr__(self, item):  # pragma: no cover - only hit by the raising fixture
        if self._raises:
            raise PermissionError("access denied")
        raise AttributeError(item)


class _Uids:
    def __init__(self, real: int) -> None:
        self.real = real


def _lister(*procs):
    def lister():
        return list(procs)

    return lister


def test_no_other_processes_is_safe() -> None:
    me = os.getpid()
    safety = probe_process_safety(
        process_lister=_lister(_Proc(me, cmdline=["openagent", "update"])),
        run_probe=lambda: [],
        pid=me,
        uid=os.getuid(),
    )
    assert safety.state is ProcessSafetyState.SAFE


def test_current_updater_process_is_excluded() -> None:
    """Our own process (and its parents) must never count as "another OpenAgent"."""

    me = os.getpid()
    safety = probe_process_safety(
        process_lister=_lister(
            _Proc(me, cmdline=["openagent", "update"]),
            _Proc(me + 1, cmdline=["uv", "tool", "install", "openagent"]),
        ),
        run_probe=lambda: [],
        pid=me,
        uid=os.getuid(),
        ancestors={me},
    )
    assert safety.state is ProcessSafetyState.SAFE


def test_idle_second_process_is_active_idle() -> None:
    safety = probe_process_safety(
        process_lister=_lister(_Proc(4242, cmdline=["openagent", "agent", "list"])),
        run_probe=lambda: [],
        pid=os.getpid(),
        uid=os.getuid(),
    )
    assert safety.state is ProcessSafetyState.ACTIVE_IDLE_PROCESS


def test_tui_process_is_active_tui() -> None:
    safety = probe_process_safety(
        process_lister=_lister(_Proc(4242, cmdline=["openagent", "tui"])),
        run_probe=lambda: [],
        pid=os.getpid(),
        uid=os.getuid(),
    )
    assert safety.state is ProcessSafetyState.ACTIVE_TUI


def test_run_process_is_active_run() -> None:
    safety = probe_process_safety(
        process_lister=_lister(_Proc(4242, cmdline=["openagent", "run", "start", "agent"])),
        run_probe=lambda: [],
        pid=os.getpid(),
        uid=os.getuid(),
    )
    assert safety.state is ProcessSafetyState.ACTIVE_RUN


def test_db_tracked_active_run_wins_over_quiet_process_table() -> None:
    """A leased run in the database is an active run even when no process matches by name."""

    safety = probe_process_safety(
        process_lister=_lister(),
        run_probe=lambda: ["run_abc (pid 991)"],
        pid=os.getpid(),
        uid=os.getuid(),
    )
    assert safety.state is ProcessSafetyState.ACTIVE_RUN


def test_run_over_tui_over_idle_precedence() -> None:
    safety = probe_process_safety(
        process_lister=_lister(
            _Proc(1, cmdline=["openagent", "agent", "list"]),
            _Proc(2, cmdline=["openagent", "tui"]),
            _Proc(3, cmdline=["openagent", "run", "resume", "run_x"]),
        ),
        run_probe=lambda: [],
        pid=os.getpid(),
        uid=os.getuid(),
    )
    assert safety.state is ProcessSafetyState.ACTIVE_RUN


# ------------------------------------------------------------------ database-backed run probe


def _runs_db(path: Path, rows: list[tuple]) -> Path:
    import sqlite3

    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE runs (id TEXT, status TEXT, active_turn_id TEXT, "
        "turn_owner_pid INTEGER, turn_owner_create_time REAL)"
    )
    connection.executemany("INSERT INTO runs VALUES (?, ?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()
    return path


def test_missing_database_reports_no_active_runs(tmp_path) -> None:
    """A fresh install has no database and nothing running — that is genuinely safe."""

    from openagent.services.update_safety import active_run_identities

    assert active_run_identities(tmp_path / "absent.db") == []


def test_running_row_is_an_active_run(tmp_path) -> None:
    from openagent.services.update_safety import active_run_identities

    db = _runs_db(tmp_path / "oa.db", [("run_1", "running", None, None, None)])
    assert active_run_identities(db) == ["run_1 (running)"]


def test_completed_runs_are_not_active(tmp_path) -> None:
    from openagent.services.update_safety import active_run_identities

    db = _runs_db(
        tmp_path / "oa.db",
        [("run_1", "completed", None, None, None), ("run_2", "cancelled", None, None, None)],
    )
    assert active_run_identities(db) == []


def test_live_lease_on_this_process_is_an_active_run(tmp_path) -> None:
    """A leased turn owned by a live process blocks, even without a matching command line."""

    import psutil

    from openagent.services.update_safety import active_run_identities

    me = psutil.Process(os.getpid())
    db = _runs_db(tmp_path / "oa.db", [("run_1", "running", "turn_1", me.pid, me.create_time())])
    assert active_run_identities(db) == ["run_1 (running)"]


def test_dead_lease_on_terminal_run_is_not_active(tmp_path) -> None:
    """A crashed owner's leftover lease needs recovery, but it is not a live writer."""

    from openagent.services.update_safety import active_run_identities

    db = _runs_db(tmp_path / "oa.db", [("run_1", "failed", "turn_1", 999999, 1.0)])
    assert active_run_identities(db) == []


def test_unreadable_database_raises_so_the_caller_fails_closed(tmp_path) -> None:
    """A database that exists but cannot be queried must not read as "no active runs"."""

    import sqlite3

    from openagent.services.update_safety import active_run_identities

    corrupt = tmp_path / "oa.db"
    corrupt.write_bytes(b"this is not a sqlite database")
    with pytest.raises(sqlite3.DatabaseError):
        active_run_identities(corrupt)

    safety = probe_process_safety(
        process_lister=_lister(),
        run_probe=lambda: active_run_identities(corrupt),
        pid=os.getpid(),
        uid=os.getuid(),
    )
    assert safety.state is ProcessSafetyState.UNKNOWN


# ------------------------------------------------------------------ fail-closed probe failures


def test_process_enumeration_failure_blocks_update() -> None:
    def boom():
        raise OSError("process listing failed")

    safety = probe_process_safety(
        process_lister=boom, run_probe=lambda: [], pid=os.getpid(), uid=os.getuid()
    )
    assert safety.state is ProcessSafetyState.UNKNOWN
    assert update_permitted(safety, force=False) is False
    assert update_permitted(safety, force=True) is False


def test_psutil_unavailable_blocks_update() -> None:
    safety = probe_process_safety(
        process_lister=None, run_probe=lambda: [], pid=os.getpid(), uid=os.getuid(), psutil_ok=False
    )
    assert safety.state is ProcessSafetyState.UNKNOWN


def test_run_probe_failure_blocks_update() -> None:
    def boom():
        raise RuntimeError("database unreadable")

    safety = probe_process_safety(
        process_lister=_lister(), run_probe=boom, pid=os.getpid(), uid=os.getuid()
    )
    assert safety.state is ProcessSafetyState.UNKNOWN


def test_permission_denied_on_same_uid_process_blocks_update() -> None:
    """A same-uid process we cannot read might be OpenAgent. Unreadable is not absent."""

    opaque = _Proc(4242, name="", cmdline=None, uid=os.getuid(), create_time=None, raises=True)
    safety = probe_process_safety(
        process_lister=_lister(opaque), run_probe=lambda: [], pid=os.getpid(), uid=os.getuid()
    )
    assert safety.state is ProcessSafetyState.UNKNOWN


def test_other_uid_opaque_process_is_ignored() -> None:
    """Another user's unreadable process cannot touch our per-user install; it must not block us."""

    other = _Proc(4242, name="", cmdline=None, uid=os.getuid() + 1, create_time=None)
    safety = probe_process_safety(
        process_lister=_lister(other), run_probe=lambda: [], pid=os.getpid(), uid=os.getuid()
    )
    assert safety.state is ProcessSafetyState.SAFE


def test_pid_reuse_is_not_treated_as_safe() -> None:
    """An OpenAgent candidate whose creation time cannot be read cannot be identity-checked."""

    candidate = _Proc(4242, cmdline=["openagent", "tui"], create_time=None)
    safety = probe_process_safety(
        process_lister=_lister(candidate), run_probe=lambda: [], pid=os.getpid(), uid=os.getuid()
    )
    assert safety.state is ProcessSafetyState.UNKNOWN


def test_unverifiable_executable_identity_blocks_update() -> None:
    candidate = _Proc(4242, name="python", cmdline=["python", "-m", "openagent"], exe=None)
    safety = probe_process_safety(
        process_lister=_lister(candidate), run_probe=lambda: [], pid=os.getpid(), uid=os.getuid()
    )
    assert safety.state is ProcessSafetyState.UNKNOWN


# ------------------------------------------------------------------ force cannot bypass danger


def _active(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("entrypoint", encoding="utf-8")
    path.chmod(0o755)
    return path


def _plan(active: Path) -> SelfUpdatePlan:
    url = f"git+{OFFICIAL_HTTPS_REMOTE}@{B}"
    return SelfUpdatePlan(
        current_version="0.1.6rc4",
        latest_version="0.1.6rc5",
        source="official-github-vcs",
        active_executable=str(active),
        resolved_executable=str(active.resolve()),
        check_method="official-github-vcs",
        update_available=True,
        can_update=True,
        channel=OpenAgentUpdateChannel.CANDIDATE,
        channel_ref="release-candidate",
        installed_commit=A,
        target_commit=B,
        package_url=url,
        commands=[["/uv", "tool", "install", "--force", "--python", "3.12", url]],
        reason="newer_version",
        detail="update",
    )


def _explode_runner(argv, timeout, limit, env, cwd):  # pragma: no cover - must never be reached
    raise AssertionError(f"no command may run while a dangerous process is live: {argv}")


@pytest.mark.parametrize("state", [ProcessSafetyState.ACTIVE_RUN, ProcessSafetyState.ACTIVE_TUI])
def test_force_cannot_bypass_active_run_or_tui(tmp_path, state) -> None:
    active = _active(tmp_path / "bin" / "openagent")
    result = perform_self_update(
        _plan(active),
        runner=_explode_runner,
        resolver=lambda _n: str(active),
        commit_reader=lambda _p: B,
        process_probe=lambda: ProcessSafety(state=state, identities=["pid 4242"], detail="live"),
        metadata_writer=lambda *a, **k: None,
        lock_path=tmp_path / "locks" / "self-update.lock",
        force=True,
    )
    assert result.ok is False
    assert result.ran is False
    assert result.error_type == "process_active"
    assert state.value in result.detail or "run" in result.detail or "TUI" in result.detail


def test_force_cannot_bypass_unknown_probe(tmp_path) -> None:
    active = _active(tmp_path / "bin" / "openagent")
    result = perform_self_update(
        _plan(active),
        runner=_explode_runner,
        resolver=lambda _n: str(active),
        commit_reader=lambda _p: B,
        process_probe=lambda: ProcessSafety(
            state=ProcessSafetyState.UNKNOWN, detail="enumeration failed"
        ),
        metadata_writer=lambda *a, **k: None,
        lock_path=tmp_path / "locks" / "self-update.lock",
        force=True,
    )
    assert result.ok is False
    assert result.ran is False
    assert result.error_type == "process_unknown"


def test_force_may_bypass_an_idle_second_process(tmp_path) -> None:
    """The one case force is for: another OpenAgent is open, but nothing is mid-write."""

    active = _active(tmp_path / "bin" / "openagent")

    def runner(argv, timeout, limit, env, cwd):
        del timeout, limit, cwd
        if argv[:3] == ["/uv", "tool", "install"]:
            return CommandResult(returncode=0, stdout="", stderr="")
        if argv[1:] == ["version"]:
            return CommandResult(returncode=0, stdout="openagent 0.1.6rc5\n", stderr="")
        if argv[1:] == ["doctor", "--json"]:
            return CommandResult(
                returncode=0, stdout=json.dumps({"checks": [], "exit_code": 0}), stderr=""
            )
        raise AssertionError(argv)

    result = perform_self_update(
        _plan(active),
        runner=runner,
        resolver=lambda _n: str(active),
        commit_reader=lambda _p: B,
        process_probe=lambda: ProcessSafety(
            state=ProcessSafetyState.ACTIVE_IDLE_PROCESS, identities=["pid 4242"], detail="idle"
        ),
        metadata_writer=lambda *a, **k: None,
        lock_path=tmp_path / "locks" / "self-update.lock",
        force=True,
    )
    assert result.ok is True
    assert result.ran is True


def test_idle_process_still_blocks_a_normal_update(tmp_path) -> None:
    active = _active(tmp_path / "bin" / "openagent")
    result = perform_self_update(
        _plan(active),
        runner=_explode_runner,
        resolver=lambda _n: str(active),
        commit_reader=lambda _p: B,
        process_probe=lambda: ProcessSafety(
            state=ProcessSafetyState.ACTIVE_IDLE_PROCESS, identities=["pid 4242"], detail="idle"
        ),
        metadata_writer=lambda *a, **k: None,
        lock_path=tmp_path / "locks" / "self-update.lock",
        force=False,
    )
    assert result.ok is False
    assert result.error_type == "process_active"


# ------------------------------------------------------------------ stale lock recovery


def test_stale_lock_without_live_process_is_recoverable(tmp_path) -> None:
    """A lock file left behind by a dead process must not wedge the updater forever.

    The lock is an OS ``flock``, so a dead owner's lock is already gone — the leftover *file*
    (with a stale pid written in it) carries no ownership. Acquiring it must simply succeed.
    """

    lock_path = tmp_path / "locks" / "self-update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("999999\n", encoding="utf-8")  # pid of a long-dead process

    with file_lock(lock_path, timeout=1.0):
        pass  # acquired without waiting out the timeout

    active = _active(tmp_path / "bin" / "openagent")

    def runner(argv, timeout, limit, env, cwd):
        del timeout, limit, cwd
        if argv[:3] == ["/uv", "tool", "install"]:
            return CommandResult(returncode=0, stdout="", stderr="")
        if argv[1:] == ["version"]:
            return CommandResult(returncode=0, stdout="openagent 0.1.6rc5\n", stderr="")
        if argv[1:] == ["doctor", "--json"]:
            return CommandResult(
                returncode=0, stdout=json.dumps({"checks": [], "exit_code": 0}), stderr=""
            )
        raise AssertionError(argv)

    result = perform_self_update(
        _plan(active),
        runner=runner,
        resolver=lambda _n: str(active),
        commit_reader=lambda _p: B,
        process_probe=lambda: ProcessSafety(state=ProcessSafetyState.SAFE, detail="clear"),
        metadata_writer=lambda *a, **k: None,
        lock_path=lock_path,
    )
    assert result.ok is True
    assert result.error_type is None
