"""A metadata write failure must be visible, not swallowed (spec §3.3).

``install.json`` is not decoration. It carries the channel, the installed version and exact commit,
and the *last accepted* version and commit — which is the baseline every future ancestry check is
measured against. An update that installs the binary but loses that file leaves an installation
that cannot prove where it came from, so the next update has to fail closed. Reporting that as a
plain success is how the operator ends up surprised later.
"""

from __future__ import annotations

import json
from pathlib import Path

from openagent.runtimes.cli.locator import CommandResult
from openagent.services.self_update import (
    OFFICIAL_HTTPS_REMOTE,
    OpenAgentUpdateChannel,
    SelfUpdatePlan,
    perform_self_update,
)
from openagent.services.update_safety import ProcessSafety, ProcessSafetyState

A = "a" * 40
B = "b" * 40


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


def _runner(argv, timeout, limit, env, cwd):
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


def _perform(tmp_path, metadata_writer):
    active = _active(tmp_path / "bin" / "openagent")
    return perform_self_update(
        _plan(active),
        runner=_runner,
        resolver=lambda _n: str(active),
        commit_reader=lambda _p: B,
        process_probe=lambda: ProcessSafety(state=ProcessSafetyState.SAFE, detail="clear"),
        metadata_writer=metadata_writer,
        lock_path=tmp_path / "locks" / "self-update.lock",
    )


def test_successful_update_reports_metadata_persisted(tmp_path) -> None:
    result = _perform(tmp_path, lambda *a, **k: None)

    assert result.ok is True
    assert result.metadata_persisted is True
    assert result.status == "installed"


def test_metadata_write_failure_is_surfaced_not_swallowed(tmp_path) -> None:
    def failing_writer(*_a, **_k):
        raise OSError(28, "No space left on device")

    result = _perform(tmp_path, failing_writer)

    # The binary really was installed and verified — that part is not walked back.
    assert result.ran is True
    assert result.verified_version == "0.1.6rc5"
    assert result.verified_commit == B
    # ...but it is not reported as an ordinary success.
    assert result.metadata_persisted is False
    assert result.status == "installed_with_metadata_warning"
    assert "--repair" in result.detail
    assert "metadata" in result.detail.lower()


def test_metadata_failure_names_the_recovery_command(tmp_path) -> None:
    def failing_writer(*_a, **_k):
        raise PermissionError("read-only file system")

    result = _perform(tmp_path, failing_writer)
    assert "openagent update --repair" in result.detail


def test_metadata_failure_detail_does_not_leak_the_exception_text(tmp_path) -> None:
    """Operator-facing text names the failure class, not raw strings from an arbitrary exception."""

    def failing_writer(*_a, **_k):
        raise OSError("/Users/someone/secret-token-path/install.json unwritable")

    result = _perform(tmp_path, failing_writer)
    assert "secret-token-path" not in result.detail
    assert "OSError" in result.detail
