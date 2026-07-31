"""Staged install, process lock, rollback, and verification for channel updates (spec §14-§18)."""

from __future__ import annotations

import json
from pathlib import Path

from openagent.runtimes.cli.locator import CommandResult
from openagent.services.self_update import (
    OFFICIAL_HTTPS_REMOTE,
    InstallMetadata,
    OpenAgentUpdateChannel,
    SelfUpdatePlan,
    perform_self_update,
)

A = "a" * 40  # previously installed commit
B = "b" * 40  # target commit


def _active(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("entrypoint", encoding="utf-8")
    path.chmod(0o755)
    return path


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


def _plan(
    active: Path,
    *,
    target_commit: str = B,
    target_version: str = "0.1.6rc4",
    installed_commit: str | None = A,
) -> SelfUpdatePlan:
    url = f"git+{OFFICIAL_HTTPS_REMOTE}@{target_commit}"
    return SelfUpdatePlan(
        current_version="0.1.6rc4",
        latest_version=target_version,
        source="official-github-vcs",
        active_executable=str(active),
        resolved_executable=str(active.resolve()),
        check_method="official-github-vcs",
        update_available=True,
        can_update=True,
        channel=OpenAgentUpdateChannel.CANDIDATE,
        channel_ref="release-candidate",
        installed_commit=installed_commit,
        target_commit=target_commit,
        package_url=url,
        commands=[
            [
                "/uv",
                "tool",
                "install",
                "--force",
                "--reinstall",
                "--refresh",
                "--python",
                "3.12",
                url,
            ]
        ],
        reason="newer_commit",
        detail="refresh",
    )


def _doctor_ok(argv):
    return _result(returncode=0, stdout=json.dumps({"checks": [], "exit_code": 0}))


def _success_runner(active: Path, *, target_version="0.1.6rc4"):
    """A runner where staging and promotion both install the target and the binary verifies clean."""

    def runner(argv, timeout, limit, env, cwd):
        del timeout, limit, cwd
        if argv[:3] == ["/uv", "tool", "install"]:
            return _result()  # staging or promote both succeed
        if argv[1:] == ["version"]:
            return _result(stdout=f"openagent {target_version}\n")
        if argv[1:] == ["doctor", "--json"]:
            return _doctor_ok(argv)
        raise AssertionError(argv)

    return runner


def test_vcs_update_success_writes_metadata(tmp_path) -> None:
    active = _active(tmp_path / "bin" / "openagent")
    meta_path = tmp_path / "home" / "install.json"
    written: list[InstallMetadata] = []

    def metadata_writer(meta, **kw):
        written.append(meta)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(meta.model_dump_json(), encoding="utf-8")

    result = perform_self_update(
        _plan(active),
        runner=_success_runner(active),
        resolver=lambda _n: str(active),
        commit_reader=lambda _p: B,  # both staged and active read back as the target commit
        process_probe=lambda: [],
        metadata_writer=metadata_writer,
        lock_path=tmp_path / "locks" / "self-update.lock",
    )

    assert result.ok is True
    assert result.ran is True
    assert result.verified_version == "0.1.6rc4"
    assert result.verified_commit == B
    assert result.doctor_exit_code == 0
    assert written and written[0].installed_commit == B
    assert written[0].last_accepted_commit == B
    assert written[0].channel is OpenAgentUpdateChannel.CANDIDATE


def test_staging_failure_leaves_active_untouched(tmp_path) -> None:
    active = _active(tmp_path / "bin" / "openagent")
    calls: list[list[str]] = []

    def runner(argv, timeout, limit, env, cwd):
        del timeout, limit, cwd
        calls.append(list(argv))
        if argv[:3] == ["/uv", "tool", "install"]:
            # Staging runs with an isolated tool dir; fail there.
            if env.get("UV_TOOL_DIR"):
                return _result(returncode=1, stderr="staging boom")
            raise AssertionError("active install must not run after a staging failure")
        raise AssertionError(argv)

    result = perform_self_update(
        _plan(active),
        runner=runner,
        resolver=lambda _n: str(active),
        commit_reader=lambda _p: B,
        process_probe=lambda: [],
        lock_path=tmp_path / "locks" / "self-update.lock",
    )

    assert result.ok is False
    assert result.error_type == "staging_failed"
    assert result.ran is False  # no active mutation happened
    # Exactly one install attempt (the staged one); the active env was never touched.
    assert sum(1 for c in calls if c[:3] == ["/uv", "tool", "install"]) == 1


def test_active_install_failure_rolls_back_to_previous_commit(tmp_path) -> None:
    active = _active(tmp_path / "bin" / "openagent")
    seen_urls: list[str] = []

    def runner(argv, timeout, limit, env, cwd):
        del timeout, limit, cwd
        if argv[:3] == ["/uv", "tool", "install"]:
            url = argv[-1]
            seen_urls.append(url)
            if env.get("UV_TOOL_DIR"):
                return _result()  # staging succeeds
            if url.endswith(B):
                return _result(returncode=1, stderr="promote boom")  # active promote fails
            return _result()  # rollback install (to A) succeeds
        if argv[1:] == ["version"]:
            if "openagent-stage-" in argv[0]:
                return _result(stdout="openagent 0.1.6rc4\n")  # staged binary verifies as target
            return _result(stdout="openagent 0.1.6rc3\n")  # old version restored after rollback
        raise AssertionError(argv)

    result = perform_self_update(
        _plan(active),
        runner=runner,
        resolver=lambda _n: str(active),
        commit_reader=lambda _p: B,
        process_probe=lambda: [],
        lock_path=tmp_path / "locks" / "self-update.lock",
    )

    assert result.ok is False
    assert result.rolled_back is True
    assert result.error_type == "update_command_failed"
    assert any(u.endswith(A) for u in seen_urls), "rollback must reinstall the previous commit"


def test_commit_mismatch_after_install_rolls_back(tmp_path) -> None:
    active = _active(tmp_path / "bin" / "openagent")

    def commit_reader(path):
        # staged reads as target B (staging passes), but the active install reads as a wrong commit.
        return B if "stage" in str(path).lower() or "tmp" in str(path).lower() else "f" * 40

    # Simpler: distinguish by call order.
    reads = iter([B, "f" * 40])  # staged=B (ok), active="f"*40 (mismatch)

    def reader(_p):
        return next(reads, "f" * 40)

    def runner(argv, timeout, limit, env, cwd):
        del timeout, limit, cwd
        if argv[:3] == ["/uv", "tool", "install"]:
            return _result()
        if argv[1:] == ["version"]:
            return _result(stdout="openagent 0.1.6rc4\n")
        raise AssertionError(argv)

    result = perform_self_update(
        _plan(active),
        runner=runner,
        resolver=lambda _n: str(active),
        commit_reader=reader,
        process_probe=lambda: [],
        lock_path=tmp_path / "locks" / "self-update.lock",
    )

    assert result.ok is False
    assert result.error_type == "commit_verification_failed"
    assert result.rolled_back is True


def test_migration_failure_keeps_new_binary_no_rollback(tmp_path) -> None:
    active = _active(tmp_path / "bin" / "openagent")
    backup = tmp_path / "openagent.db.pre-migration.bak"
    install_urls: list[str] = []

    def runner(argv, timeout, limit, env, cwd):
        del timeout, limit, cwd
        if argv[:3] == ["/uv", "tool", "install"]:
            install_urls.append(argv[-1])
            return _result()
        if argv[1:] == ["version"]:
            return _result(stdout="openagent 0.1.6rc4\n")
        if argv[1:] == ["doctor", "--json"]:
            return _result(
                returncode=3,
                stdout=json.dumps(
                    {"checks": [{"data": {"backup_path": str(backup)}}], "exit_code": 3}
                ),
            )
        raise AssertionError(argv)

    result = perform_self_update(
        _plan(active),
        runner=runner,
        resolver=lambda _n: str(active),
        commit_reader=lambda _p: B,
        process_probe=lambda: [],
        lock_path=tmp_path / "locks" / "self-update.lock",
    )

    assert result.ok is False
    assert result.error_type == "migration_failed"
    assert result.rolled_back is False
    assert result.backup_path == str(backup)
    # No rollback install to A — an old binary may not read the migrated schema (spec §17.2).
    assert not any(u.endswith(A) for u in install_urls)


def test_active_process_blocks_update_unless_forced(tmp_path) -> None:
    active = _active(tmp_path / "bin" / "openagent")
    installs: list[str] = []

    def runner(argv, timeout, limit, env, cwd):
        del timeout, limit, cwd
        if argv[:3] == ["/uv", "tool", "install"]:
            installs.append(argv[-1])
            return _result()
        if argv[1:] == ["version"]:
            return _result(stdout="openagent 0.1.6rc4\n")
        if argv[1:] == ["doctor", "--json"]:
            return _doctor_ok(argv)
        raise AssertionError(argv)

    blocked = perform_self_update(
        _plan(active),
        runner=runner,
        resolver=lambda _n: str(active),
        commit_reader=lambda _p: B,
        process_probe=lambda: ["pid 4242"],
        lock_path=tmp_path / "locks" / "self-update.lock",
    )
    assert blocked.ok is False
    assert blocked.error_type == "process_active"
    assert installs == []  # nothing installed while another process is live

    forced = perform_self_update(
        _plan(active),
        runner=runner,
        resolver=lambda _n: str(active),
        commit_reader=lambda _p: B,
        process_probe=lambda: ["pid 4242"],
        metadata_writer=lambda *a, **k: None,  # never touch the real ~/.openagent
        lock_path=tmp_path / "locks" / "self-update.lock",
        force=True,
    )
    assert forced.ok is True


def test_concurrent_update_reports_in_progress(tmp_path) -> None:
    from openagent.security.file_lock import file_lock

    active = _active(tmp_path / "bin" / "openagent")
    lock = tmp_path / "locks" / "self-update.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)

    with file_lock(lock):  # simulate another updater already holding the lock
        result = perform_self_update(
            _plan(active),
            runner=_success_runner(active),
            resolver=lambda _n: str(active),
            commit_reader=lambda _p: B,
            process_probe=lambda: [],
            lock_path=lock,
            lock_timeout=0.5,  # fail fast instead of waiting the full update-lock timeout
        )

    assert result.ok is False
    assert result.error_type == "update_in_progress"
    assert "already running" in result.detail
