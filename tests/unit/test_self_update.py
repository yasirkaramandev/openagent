from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openagent.cli.app import app
from openagent.runtimes.cli.locator import CommandResult
from openagent.services.self_update import (
    SelfUpdatePlan,
    SelfUpdateResult,
    check_self_update,
    perform_self_update,
)


@pytest.fixture(autouse=True)
def _isolate_install_metadata(monkeypatch):
    """These legacy tests model installs *without* ``install.json``; never read the real home."""

    monkeypatch.setattr(
        "openagent.services.self_update.read_install_metadata", lambda *a, **k: None
    )


def _active(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("entrypoint", encoding="utf-8")
    path.chmod(0o755)
    return path


def _source_checkout(root: Path, *, version: str = "0.1.4") -> Path:
    """A source checkout that declares ``version`` in the same layout the real tree uses."""

    root.mkdir(parents=True, exist_ok=True)
    (root / "setup.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    pkg = root / "src" / "openagent"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    return root


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


def test_index_install_uses_owning_python_not_path_pip(tmp_path: Path) -> None:
    active = _active(tmp_path / "bin" / "openagent")
    python = tmp_path / "venv" / "bin" / "python"

    plan = check_self_update(
        current_version="0.1.3",
        active_executable=str(active),
        python_executable=str(python),
        prefix=str(tmp_path / "venv"),
        direct_url=None,
        environ={"PATH": ""},
        fetcher=lambda *_args: {"info": {"version": "0.1.4"}},
    )

    assert plan.source == "pip"
    assert plan.update_available is True
    assert plan.commands == [[str(python), "-m", "pip", "install", "--upgrade", "openagent"]]


def test_uv_tool_install_uses_uv_tool_upgrade(tmp_path: Path, monkeypatch) -> None:
    active = _active(tmp_path / "bin" / "openagent")
    tool_root = tmp_path / "uv" / "tools"
    prefix = tool_root / "openagent"
    uv = tmp_path / "bin" / "uv"
    _active(uv)
    monkeypatch.setattr(
        "openagent.services.self_update.shutil.which",
        lambda name, path=None: str(uv) if name == "uv" else None,
    )

    def runner(argv, timeout, limit, env, cwd):
        del timeout, limit, env, cwd
        assert argv == [str(uv), "tool", "dir"]
        return _result(stdout=f"{tool_root}\n")

    plan = check_self_update(
        current_version="0.1.3",
        active_executable=str(active),
        python_executable=str(prefix / "bin" / "python"),
        prefix=str(prefix),
        direct_url=None,
        runner=runner,
        environ={"PATH": str(uv.parent)},
        fetcher=lambda *_args: {"info": {"version": "0.1.4"}},
    )

    assert plan.source == "uv-tool"
    assert plan.commands == [[str(uv), "tool", "upgrade", "openagent"]]


def _git_runner(
    root: Path,
    *,
    dirty: bool = False,
    head_after_pull: str = "b" * 40,
    origin: str = "git@github.com:yasirkaramandev/openagent.git",
):
    def runner(argv, timeout, limit, env, cwd):
        del timeout, limit, env
        assert cwd == root
        tail = list(argv[3:])
        if tail == ["rev-parse", "--show-toplevel"]:
            return _result(stdout=f"{root}\n")
        if tail == ["remote", "get-url", "origin"]:
            return _result(stdout=f"{origin}\n")
        if tail == ["branch", "--show-current"]:
            return _result(stdout="main\n")
        if tail == ["status", "--porcelain", "--untracked-files=normal"]:
            return _result(stdout=" M README.md\n" if dirty else "")
        if tail == ["rev-parse", "HEAD"]:
            return _result(stdout=f"{head_after_pull}\n")
        if tail == ["ls-remote", "--heads", "origin", "main"]:
            return _result(stdout=f"{'b' * 40}\trefs/heads/main\n")
        raise AssertionError(argv)

    return runner


# The source-checkout tests below exercise the opt-in developer workflow (``local_dev=True``). As of
# the checkout-independent updater (spec §5, §10), a legacy ``file://`` install *without* that opt-in
# no longer consults the checkout at all — it migrates to the official channel — so these tests set
# ``local_dev=True`` to reach the source-checkout code path they cover. See
# ``test_self_update_channels.py`` for the default migration behavior.
def test_official_clean_source_checkout_fast_forwards_and_reinstalls(tmp_path: Path) -> None:
    root = _source_checkout(tmp_path / "Open Agent Source", version="0.1.4")
    active = _active(tmp_path / "tool" / "openagent")

    plan = check_self_update(
        current_version="0.1.3",
        active_executable=str(active),
        direct_url={"url": root.as_uri(), "dir_info": {}},
        runner=_git_runner(root, head_after_pull="a" * 40),
        platform="darwin",
        local_dev=True,
    )

    assert plan.source == "source-checkout"
    assert plan.can_update is True
    assert plan.update_available is True
    assert plan.revision_update_available is True
    assert plan.commands[0][-4:] == ["pull", "--ff-only", "origin", "main"]
    assert plan.commands[1] == ["sh", str(root / "setup.sh")]


def test_dirty_or_non_official_source_is_blocked(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "setup.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    active = _active(tmp_path / "bin" / "openagent")

    dirty = check_self_update(
        active_executable=str(active),
        direct_url={"url": root.as_uri(), "dir_info": {}},
        runner=_git_runner(root, dirty=True),
        platform="linux",
        local_dev=True,
    )
    remote = check_self_update(
        active_executable=str(active),
        direct_url={"url": "https://example.invalid/openagent.whl", "archive_info": {}},
    )

    assert dirty.can_update is False
    assert "local changes" in dirty.detail
    assert remote.source == "unsupported"
    assert remote.can_update is False


def test_source_update_rejects_unencrypted_official_origin(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "setup.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    active = _active(tmp_path / "bin" / "openagent")

    plan = check_self_update(
        active_executable=str(active),
        direct_url={"url": root.as_uri(), "dir_info": {}},
        runner=_git_runner(root, origin="http://github.com/yasirkaramandev/openagent.git"),
        platform="linux",
        local_dev=True,
    )

    assert plan.can_update is False
    assert "official" in plan.detail


# ---------------------------------------------------------------- installation drift (spec §3)


def _current_checkout_plan(
    tmp_path: Path,
    *,
    binary_version: str,
    source_version: str,
    repair: bool = False,
) -> SelfUpdatePlan:
    """A source checkout whose HEAD is level with origin/main (local SHA == remote SHA)."""

    root = _source_checkout(tmp_path / "checkout", version=source_version)
    active = _active(tmp_path / "tool" / "openagent")
    return check_self_update(
        current_version=binary_version,
        active_executable=str(active),
        direct_url={"url": root.as_uri(), "dir_info": {}},
        runner=_git_runner(root, head_after_pull="b" * 40),  # HEAD == ls-remote "b"*40
        platform="linux",
        repair=repair,
        local_dev=True,
    )


def test_source_checkout_reinstalls_when_head_is_current_but_binary_is_old(tmp_path: Path) -> None:
    """The reported bug: checkout is current, but the binary on PATH is a stale copy."""

    plan = _current_checkout_plan(tmp_path, binary_version="0.1.4", source_version="0.1.6rc2")

    assert plan.can_update is True
    assert plan.update_available is True
    assert plan.installation_drift is True
    assert plan.revision_update_available is False
    assert plan.needs_reinstall is True
    # The installer runs; there is no fast-forward because the checkout is already current.
    assert plan.commands == [["sh", str(Path(plan.checkout_root) / "setup.sh")]]
    assert not any(cmd[-4:] == ["pull", "--ff-only", "origin", "main"] for cmd in plan.commands)


def test_source_checkout_is_current_only_when_head_and_binary_both_match(tmp_path: Path) -> None:
    plan = _current_checkout_plan(tmp_path, binary_version="0.1.6rc2", source_version="0.1.6rc2")

    assert plan.can_update is True
    assert plan.update_available is False
    assert plan.needs_reinstall is False
    assert plan.installation_drift is False
    assert plan.revision_update_available is False


def test_source_checkout_reinstalls_when_active_version_is_unparseable(tmp_path: Path) -> None:
    """Case E: an unreadable active version must not be assumed current."""

    plan = _current_checkout_plan(
        tmp_path, binary_version="dev-build-unknown", source_version="0.1.6rc2"
    )

    assert plan.installation_drift is True
    assert plan.needs_reinstall is True
    assert plan.update_available is True


def test_source_checkout_update_reports_installation_drift(tmp_path: Path) -> None:
    plan = _current_checkout_plan(tmp_path, binary_version="0.1.4", source_version="0.1.6rc2")

    assert plan.source_version == "0.1.6rc2"
    assert plan.installation_drift is True
    assert "0.1.4" in plan.detail and "0.1.6rc2" in plan.detail


def test_source_checkout_without_parseable_source_version_is_blocked(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "setup.sh").write_text("#!/bin/sh\n", encoding="utf-8")  # no src/openagent/__init__.py
    active = _active(tmp_path / "tool" / "openagent")

    plan = check_self_update(
        current_version="0.1.4",
        active_executable=str(active),
        direct_url={"url": root.as_uri(), "dir_info": {}},
        runner=_git_runner(root, head_after_pull="b" * 40),
        platform="linux",
        local_dev=True,
    )

    assert plan.can_update is False
    assert "parseable OpenAgent version" in plan.detail


def test_source_checkout_repair_runs_installer_without_remote_revision_change(
    tmp_path: Path,
) -> None:
    """--repair reinstalls even when the version already matches, with no fast-forward."""

    plan = _current_checkout_plan(
        tmp_path, binary_version="0.1.6rc2", source_version="0.1.6rc2", repair=True
    )

    assert plan.can_update is True
    assert plan.update_available is True
    assert plan.needs_reinstall is True
    assert plan.revision_update_available is False
    assert plan.commands == [["sh", str(Path(plan.checkout_root) / "setup.sh")]]


def test_source_checkout_repair_verifies_exact_active_binary(tmp_path: Path) -> None:
    """A repair plan runs through perform_self_update and verifies the exact active binary."""

    plan = _current_checkout_plan(
        tmp_path, binary_version="0.1.4", source_version="0.1.6rc2", repair=True
    )
    root = Path(plan.checkout_root)
    active = Path(plan.active_executable)

    def runner(argv, timeout, limit, env, cwd):
        del timeout, limit, env
        tail = list(argv[3:]) if argv[0] == "git" else []
        if argv[0] == "sh":  # the installer
            return _result()
        if tail == ["rev-parse", "HEAD"]:
            assert cwd == root
            return _result(stdout=f"{'b' * 40}\n")  # HEAD already at the verified remote revision
        if argv[1:] == ["version"]:
            return _result(stdout="openagent 0.1.6rc2\n")
        if argv[1:] == ["doctor", "--json"]:
            return _result(returncode=0, stdout=json.dumps({"checks": [], "exit_code": 0}))
        raise AssertionError(argv)

    result = perform_self_update(plan, runner=runner, resolver=lambda _name: str(active))

    assert result.ok is True
    assert result.ran is True
    assert result.verified_version == "0.1.6rc2"


def test_cli_update_does_not_return_early_when_installation_drift_exists(
    tmp_path: Path, monkeypatch
) -> None:
    """`openagent update --yes` must actually reinstall a drifted binary, not report 'current'."""

    active = _active(tmp_path / "tool" / "openagent")
    plan = SelfUpdatePlan(
        current_version="0.1.4",
        latest_version="0.1.6rc2",
        source="source-checkout",
        active_executable=str(active),
        resolved_executable=str(active.resolve()),
        check_method="git-origin-main",
        update_available=True,
        can_update=True,
        commands=[["sh", "setup.sh"]],
        checkout_root=str(tmp_path / "checkout"),
        source_version="0.1.6rc2",
        installation_drift=True,
        needs_reinstall=True,
        detail="installed binary reports 0.1.4 but the checkout is 0.1.6rc2",
    )
    performed: list[SelfUpdatePlan] = []

    def fake_perform(passed_plan, **_kwargs):
        performed.append(passed_plan)
        return SelfUpdateResult(plan=passed_plan, ok=True, ran=True, verified_version="0.1.6rc2")

    monkeypatch.setattr("openagent.services.self_update.check_self_update", lambda **_kw: plan)
    monkeypatch.setattr("openagent.services.self_update.perform_self_update", fake_perform)

    result = CliRunner().invoke(app, ["update", "--yes", "--json"])

    assert result.exit_code == 0, result.output
    assert performed, "perform_self_update was never called — the CLI returned early on drift"
    assert performed[0].installation_drift is True


def _index_plan(active: Path) -> SelfUpdatePlan:
    return SelfUpdatePlan(
        current_version="0.1.3",
        latest_version="0.1.4",
        source="pip",
        active_executable=str(active),
        resolved_executable=str(active.resolve()),
        check_method="pypi-json",
        update_available=True,
        can_update=True,
        commands=[["python", "-m", "pip", "install", "--upgrade", "openagent"]],
        detail="0.1.3 -> 0.1.4",
    )


def test_update_verifies_exact_path_version_and_doctor_warning(tmp_path: Path) -> None:
    active = _active(tmp_path / "bin" / "openagent")

    def runner(argv, timeout, limit, env, cwd):
        del timeout, limit, env, cwd
        if argv[0] == "python":
            return _result()
        if argv[1:] == ["version"]:
            return _result(stdout="openagent 0.1.4\n")
        if argv[1:] == ["doctor", "--json"]:
            return _result(returncode=1, stdout=json.dumps({"checks": [], "exit_code": 1}))
        raise AssertionError(argv)

    result = perform_self_update(
        _index_plan(active),
        runner=runner,
        resolver=lambda _name: str(active),
    )

    assert result.ok is True
    assert result.ran is True
    assert result.verified_version == "0.1.4"
    assert result.doctor_exit_code == 1


def test_update_refuses_shadowed_path_after_mutation(tmp_path: Path) -> None:
    active = _active(tmp_path / "new" / "openagent")
    old = _active(tmp_path / "old" / "openagent")

    result = perform_self_update(
        _index_plan(active),
        runner=lambda *_args: _result(),
        resolver=lambda _name: str(old),
    )

    assert result.ok is False
    assert result.error_type == "path_conflict"


def test_update_reports_migration_failure_and_backup(tmp_path: Path) -> None:
    active = _active(tmp_path / "bin" / "openagent")
    backup = tmp_path / "openagent.db.pre-migration.bak"

    def runner(argv, timeout, limit, env, cwd):
        del timeout, limit, env, cwd
        if argv[0] == "python":
            return _result()
        if argv[1:] == ["version"]:
            return _result(stdout="openagent 0.1.4\n")
        if argv[1:] == ["doctor", "--json"]:
            payload = {
                "checks": [{"data": {"backup_path": str(backup)}}],
                "exit_code": 3,
            }
            return _result(returncode=3, stdout=json.dumps(payload))
        raise AssertionError(argv)

    result = perform_self_update(
        _index_plan(active),
        runner=runner,
        resolver=lambda _name: str(active),
    )

    assert result.ok is False
    assert result.error_type == "migration_failed"
    assert result.doctor_exit_code == 3
    assert result.backup_path == str(backup)


def test_update_failure_output_is_bounded_and_redacted(tmp_path: Path) -> None:
    active = _active(tmp_path / "bin" / "openagent")
    secret = "sk-12345678901234567890"

    result = perform_self_update(
        _index_plan(active),
        runner=lambda *_args: _result(returncode=1, stderr=f"token={secret}"),
        resolver=lambda _name: str(active),
    )

    assert result.ok is False
    assert secret not in result.detail
    assert "[REDACTED]" in result.detail


def test_cli_update_dry_run_and_confirmed_json(monkeypatch, tmp_path: Path) -> None:
    active = _active(tmp_path / "bin" / "openagent")
    plan = _index_plan(active)
    completed = SelfUpdateResult(
        plan=plan.model_copy(update={"current_version": "0.1.4", "update_available": False}),
        ok=True,
        ran=True,
        verified_version="0.1.4",
        doctor_exit_code=0,
        detail="updated to 0.1.4",
    )
    monkeypatch.setattr("openagent.services.self_update.check_self_update", lambda **_kw: plan)
    monkeypatch.setattr(
        "openagent.services.self_update.perform_self_update",
        lambda value, **_kw: completed,
    )

    runner = CliRunner()
    dry = runner.invoke(app, ["update", "--dry-run", "--json"])
    actual = runner.invoke(app, ["update", "--yes", "--json"])

    assert dry.exit_code == 0
    assert json.loads(dry.stdout)["plan"]["commands"] == plan.commands
    assert actual.exit_code == 0
    assert json.loads(actual.stdout)["verified_version"] == "0.1.4"
