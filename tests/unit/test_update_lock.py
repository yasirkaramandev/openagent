"""Foreign updater lock inspection and stale-lock recovery (spec §21.8).

Two failures are possible here and they are not symmetric. Refusing to clear a genuinely abandoned
lock is an annoyance the user can work around. Clearing one that a live updater is holding produces
the corrupt half-written install the lock exists to prevent — so every ambiguous case resolves
towards refusing.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from openagent.runtimes.cli.update_lock import (
    STALE_AFTER,
    LockOwnerState,
    StaleLockError,
    UpdateLockReport,
    clear_stale_lock,
    inspect_update_lock,
)


def write_lock(path: Path, body: str, *, age: timedelta = timedelta(0)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    if age:
        stamp = (datetime.now(timezone.utc) - age).timestamp()
        os.utime(path, (stamp, stamp))
    return path


class TestInspection:
    def test_an_absent_lock_is_not_present_and_blocks_nothing(self, tmp_path: Path) -> None:
        report = inspect_update_lock(tmp_path / "update.lock")
        assert report.present is False
        assert report.blocks_update is False
        assert report.stale is False

    def test_a_lock_blocks_updates_whatever_else_is_known(self, tmp_path: Path) -> None:
        report = inspect_update_lock(write_lock(tmp_path / "update.lock", "{}"))
        assert report.present is True
        assert report.blocks_update is True

    @pytest.mark.parametrize(
        "body",
        ['{"pid": 4242}', "pid=4242", "pid: 4242", "4242"],
    )
    def test_a_pid_is_read_from_any_of_the_shapes_a_foreign_lock_might_use(
        self, tmp_path: Path, body: str
    ) -> None:
        report = inspect_update_lock(write_lock(tmp_path / "update.lock", body))
        assert report.pid == 4242

    def test_an_unrecognised_format_leaves_the_owner_unknown(self, tmp_path: Path) -> None:
        """Unknown, not stale. Guessing permissively here is the dangerous guess."""

        report = inspect_update_lock(write_lock(tmp_path / "update.lock", "locked by someone"))
        assert report.pid is None
        assert report.owner_state == LockOwnerState.UNKNOWN
        assert report.unreadable_reason
        assert report.stale is False

    def test_an_empty_lock_is_unreadable_not_abandoned(self, tmp_path: Path) -> None:
        report = inspect_update_lock(
            write_lock(tmp_path / "update.lock", "", age=timedelta(days=7))
        )
        assert report.stale is False
        assert report.unreadable_reason

    def test_a_live_owner_is_reported_as_alive(self, tmp_path: Path) -> None:
        report = inspect_update_lock(
            write_lock(tmp_path / "update.lock", json.dumps({"pid": os.getpid()}))
        )
        assert report.owner_state == LockOwnerState.ALIVE
        assert report.owner_create_time is not None
        assert report.owner_name

    def test_the_owners_command_line_is_never_recorded(self, tmp_path: Path) -> None:
        """An updater's argv can carry a token; the process *name* is enough to recognise it."""

        report = inspect_update_lock(
            write_lock(tmp_path / "update.lock", json.dumps({"pid": os.getpid()}))
        )
        rendered = json.dumps(report.to_dict())
        assert "cmdline" not in rendered
        assert "--" not in (report.owner_name or "")

    def test_a_dead_owner_is_reported_as_gone(self, tmp_path: Path) -> None:
        report = inspect_update_lock(
            write_lock(tmp_path / "update.lock", json.dumps({"pid": _dead_pid()}))
        )
        assert report.owner_state == LockOwnerState.GONE

    def test_age_is_measured_from_the_files_mtime(self, tmp_path: Path) -> None:
        report = inspect_update_lock(
            write_lock(tmp_path / "update.lock", "{}", age=timedelta(hours=3))
        )
        assert report.age is not None
        assert timedelta(hours=2, minutes=30) < report.age < timedelta(hours=3, minutes=30)

    def test_a_symlink_where_the_lock_belongs_is_refused_not_followed(self, tmp_path: Path) -> None:
        real = write_lock(tmp_path / "real.lock", json.dumps({"pid": _dead_pid()}))
        link = tmp_path / "update.lock"
        link.symlink_to(real)
        report = inspect_update_lock(link)
        assert report.unreadable_reason
        assert report.stale is False


class TestStaleness:
    def test_a_live_owner_is_never_stale_however_old_the_file_is(self, tmp_path: Path) -> None:
        """A long-running update is not an abandoned one. Liveness beats age."""

        report = inspect_update_lock(
            write_lock(
                tmp_path / "update.lock",
                json.dumps({"pid": os.getpid()}),
                age=timedelta(days=30),
            )
        )
        assert report.owner_state == LockOwnerState.ALIVE
        assert report.stale is False

    def test_a_dead_owner_alone_is_not_enough(self, tmp_path: Path) -> None:
        """A just-started updater may not have written its pid yet; age is the second gate."""

        report = inspect_update_lock(
            write_lock(tmp_path / "update.lock", json.dumps({"pid": _dead_pid()}))
        )
        assert report.owner_state == LockOwnerState.GONE
        assert report.stale is False

    def test_a_dead_owner_plus_age_is_stale(self, tmp_path: Path) -> None:
        report = inspect_update_lock(
            write_lock(
                tmp_path / "update.lock",
                json.dumps({"pid": _dead_pid()}),
                age=STALE_AFTER + timedelta(hours=1),
            )
        )
        assert report.stale is True
        assert "abandoned" in report.summary()

    def test_an_uninspectable_owner_is_not_stale(self, tmp_path: Path) -> None:
        report = UpdateLockReport(
            path=tmp_path / "update.lock",
            present=True,
            pid=1,
            owner_state=LockOwnerState.UNKNOWN,
            age=timedelta(days=30),
        )
        assert report.stale is False


class TestRecovery:
    def _stale(self, tmp_path: Path) -> UpdateLockReport:
        return inspect_update_lock(
            write_lock(
                tmp_path / "update.lock",
                json.dumps({"pid": _dead_pid()}),
                age=STALE_AFTER + timedelta(hours=1),
            )
        )

    def test_a_stale_lock_is_removed_with_approval(self, tmp_path: Path) -> None:
        report = self._stale(tmp_path)
        assert clear_stale_lock(report, approved=True) is True
        assert not report.path.exists()

    def test_approval_is_required_even_when_it_is_stale(self, tmp_path: Path) -> None:
        """This deletes a file OpenAgent does not own, in another tool's directory."""

        report = self._stale(tmp_path)
        with pytest.raises(StaleLockError, match="approval"):
            clear_stale_lock(report)
        assert report.path.exists()

    def test_a_live_lock_is_refused_even_with_approval(self, tmp_path: Path) -> None:
        """The failure that matters: removing a held lock produces the corrupt install."""

        report = inspect_update_lock(
            write_lock(tmp_path / "update.lock", json.dumps({"pid": os.getpid()}))
        )
        with pytest.raises(StaleLockError, match="not provably abandoned"):
            clear_stale_lock(report, approved=True)
        assert report.path.exists()

    def test_an_unreadable_lock_is_refused(self, tmp_path: Path) -> None:
        report = inspect_update_lock(
            write_lock(tmp_path / "update.lock", "opaque", age=timedelta(days=30))
        )
        with pytest.raises(StaleLockError):
            clear_stale_lock(report, approved=True)

    def test_staleness_is_re_derived_and_not_taken_on_trust(self, tmp_path: Path) -> None:
        """A hand-built report must not be able to force the removal of a live lock."""

        path = write_lock(tmp_path / "update.lock", json.dumps({"pid": os.getpid()}))
        forged = UpdateLockReport(
            path=path,
            present=True,
            pid=os.getpid(),
            owner_state=LockOwnerState.ALIVE,
            age=timedelta(days=365),
        )
        with pytest.raises(StaleLockError):
            clear_stale_lock(forged, approved=True)
        assert path.exists()

    def test_clearing_an_absent_lock_is_a_no_op(self, tmp_path: Path) -> None:
        report = inspect_update_lock(tmp_path / "update.lock")
        assert clear_stale_lock(report, approved=True) is False

    def test_a_lock_removed_by_someone_else_meanwhile_is_success(self, tmp_path: Path) -> None:
        report = self._stale(tmp_path)
        report.path.unlink()
        assert clear_stale_lock(report, approved=True) is True


def _dead_pid() -> int:
    """A pid that is not running.

    Found by spawning and reaping a child, so the pid is genuinely dead rather than a large number
    guessed to be free — a guess that fails on a busy machine.
    """

    import subprocess

    proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
    proc.wait()
    return proc.pid
