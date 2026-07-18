from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from openagent.core.models import (
    CliInstallation,
    CliInstallSource,
    CliUpdateState,
    CliUpdateStatus,
)
from openagent.runtimes.cli.installations import detect_install_source, inspect_installation
from openagent.runtimes.cli.locator import (
    CommandResult,
    ExecutableCandidate,
    candidate_paths,
    locate_candidates,
)
from openagent.runtimes.cli.updates import check_update, perform_update


def _executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _probe_runner(argv, timeout, limit):
    del timeout, limit
    if argv[:3] == ["npm", "prefix", "-g"]:
        return CommandResult(returncode=1)
    if argv[-1] == "--version":
        return CommandResult(returncode=0, stdout=f"{Path(argv[0]).name} 1.2.3\n")
    if argv[-1] == "--help":
        return CommandResult(
            returncode=0,
            stdout="--output-format --model --permission-mode",
        )
    return CommandResult(returncode=1)


def test_locator_prefers_first_path_copy_and_realpath_dedupes(tmp_path: Path):
    first = _executable(tmp_path / "one" / "codex")
    duplicate = tmp_path / "two" / "codex"
    duplicate.parent.mkdir()
    duplicate.symlink_to(first)
    shadowed = _executable(tmp_path / "three" / "codex")
    env = {"PATH": os.pathsep.join(str(p.parent) for p in (first, duplicate, shadowed))}

    location = locate_candidates("codex", env=env, home=tmp_path / "home", runner=_probe_runner)

    assert location.active_executable == str(first)
    assert location.resolved_executable == str(first.resolve())
    assert location.shadowed_executables == [str(shadowed)]
    assert location.path_conflict is True
    assert len([candidate for candidate in location.candidates if candidate.valid]) == 2


def test_locator_finds_native_path_when_launch_path_is_stale(tmp_path: Path):
    native = _executable(tmp_path / ".local" / "bin" / "claude")

    location = locate_candidates(
        "claude", env={"PATH": "/stale"}, home=tmp_path, runner=_probe_runner
    )

    assert location.active_executable == str(native)
    assert (
        next(candidate for candidate in location.candidates if candidate.active).origin == "native"
    )


def test_windows_candidates_include_cmd_exe_and_winget(tmp_path: Path):
    env = {
        "PATH": r"C:\npm;C:\tools",
        "PATHEXT": ".EXE;.CMD",
        "APPDATA": r"C:\Users\me\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\me\AppData\Local",
        "USERPROFILE": r"C:\Users\me",
    }

    paths = candidate_paths(
        "claude", env=env, home=tmp_path, platform="win32", runner=_probe_runner
    )
    rendered = {str(path).replace("\\", "/").lower() for path, _origin in paths}

    assert any(path.endswith("claude.cmd") for path in rendered)
    assert any("winget/links/claude.exe" in path for path in rendered)
    assert any(".local/bin/claude.exe" in path for path in rendered)


def test_claude_desktop_like_executable_is_rejected(tmp_path: Path):
    desktop = _executable(tmp_path / "bin" / "Claude.exe")

    def desktop_runner(argv, timeout, limit):
        del timeout, limit
        if argv[:3] == ["npm", "prefix", "-g"]:
            return CommandResult(returncode=1)
        if argv[-1] == "--version":
            return CommandResult(returncode=0, stdout="Claude Desktop 1.0")
        return CommandResult(returncode=0, stdout="desktop flags only")

    location = locate_candidates(
        "claude",
        explicit_path=str(desktop),
        env={"PATH": ""},
        home=tmp_path / "home",
        platform="win32",
        runner=desktop_runner,
    )

    assert location.active_executable is None
    assert location.desktop_conflict is True
    assert "not the Claude Code CLI" in location.candidates[0].detail


def test_directory_and_broken_symlink_are_invalid(tmp_path: Path):
    directory = tmp_path / "codex"
    directory.mkdir()
    broken = tmp_path / "broken" / "codex"
    broken.parent.mkdir()
    broken.symlink_to(tmp_path / "missing")

    directory_result = locate_candidates(
        "codex",
        explicit_path=str(directory),
        env={"PATH": ""},
        home=tmp_path / "home",
        runner=_probe_runner,
    )
    broken_result = locate_candidates(
        "codex",
        explicit_path=str(broken),
        env={"PATH": ""},
        home=tmp_path / "home",
        runner=_probe_runner,
    )

    assert directory_result.candidates[0].valid is False
    assert "directory" in directory_result.candidates[0].detail
    assert broken_result.candidates[0].valid is False
    assert "symlink" in broken_result.candidates[0].detail


@pytest.mark.parametrize(
    ("path", "origin", "expected"),
    [
        ("/opt/homebrew/Caskroom/codex/1.0/codex", "path", CliInstallSource.HOMEBREW_CASK),
        (
            "/opt/homebrew/Cellar/codex/1.0/bin/codex",
            "path",
            CliInstallSource.HOMEBREW_FORMULA_LEGACY,
        ),
        ("/usr/lib/node_modules/@openai/codex/bin/codex", "npm", CliInstallSource.NPM),
        (
            r"C:\Users\me\AppData\Local\Microsoft\WinGet\Links\claude.exe",
            "winget",
            CliInstallSource.WINGET,
        ),
    ],
)
def test_install_source_uses_realpath_and_package_evidence(path, origin, expected):
    candidate = ExecutableCandidate(
        path=path,
        resolved_path=path,
        origin=origin,
        valid=True,
        active=True,
        version="1.0.0",
    )
    assert detect_install_source("codex" if "codex" in path else "claude", candidate) is expected


def test_inspection_persists_active_and_shadowed_paths():
    from openagent.runtimes.cli.locator import CliLocation

    active = ExecutableCandidate(
        path="/bin/codex",
        resolved_path="/real/codex",
        origin="npm",
        valid=True,
        active=True,
        version="codex-cli 1.0.0",
    )
    location = CliLocation(
        cli_type="codex",
        active_executable=active.path,
        resolved_executable=active.resolved_path,
        shadowed_executables=["/other/codex"],
        candidates=[active],
        path_conflict=True,
    )

    installation = inspect_installation("codex", location, adapter="codex-json")

    assert installation is not None
    assert installation.executable == "/bin/codex"
    assert installation.resolved_executable == "/real/codex"
    assert installation.install_source is CliInstallSource.NPM
    assert installation.shadowed_executables == ["/other/codex"]


def _installation(
    tmp_path: Path,
    *,
    cli_type: str = "codex",
    source: CliInstallSource = CliInstallSource.NPM,
    shadowed: list[str] | None = None,
) -> CliInstallation:
    binary = _executable(tmp_path / cli_type)
    return CliInstallation(
        id=f"cli_{cli_type}",
        type=cli_type,
        executable=str(binary),
        resolved_executable=str(binary),
        version=f"{cli_type} 1.0.0",
        install_source=source,
        shadowed_executables=shadowed or [],
    )


def test_npm_update_check_uses_machine_readable_registry_metadata(tmp_path: Path):
    installation = _installation(tmp_path)

    def runner(argv, timeout, limit):
        del timeout, limit
        assert argv == ["npm", "view", "@openai/codex", "version", "--json"]
        return CommandResult(returncode=0, stdout=json.dumps("1.2.0"))

    status = check_update(installation, runner=runner)

    assert status.state is CliUpdateState.AVAILABLE
    assert status.latest_version == "1.2.0"
    assert status.check_method == "npm-registry"


def test_homebrew_and_winget_update_checks_are_source_specific(tmp_path: Path):
    brew = _installation(tmp_path / "brew", source=CliInstallSource.HOMEBREW_CASK)
    winget = _installation(tmp_path / "winget", cli_type="claude", source=CliInstallSource.WINGET)

    def runner(argv, timeout, limit):
        del timeout, limit
        if argv[0] == "brew":
            return CommandResult(
                returncode=0,
                stdout=json.dumps({"casks": [{"version": "1.3.0"}]}),
            )
        assert argv[:4] == ["winget", "list", "--id", "Anthropic.ClaudeCode"]
        return CommandResult(returncode=0, stdout="Name Id Version Available\nClaude x 1.0.0 1.4.0")

    assert check_update(brew, runner=runner).latest_version == "1.3.0"
    assert check_update(winget, runner=runner).latest_version == "1.4.0"


def test_unknown_conflicting_and_active_run_updates_fail_closed(tmp_path: Path):
    unknown = _installation(tmp_path / "unknown", source=CliInstallSource.UNKNOWN)
    conflict = _installation(tmp_path / "conflict", shadowed=["/other/codex"])
    status = CliUpdateStatus(
        current_version="1.0.0",
        latest_version="1.1.0",
        update_available=True,
        state=CliUpdateState.AVAILABLE,
        install_source=unknown.install_source,
        active_executable=unknown.executable,
        resolved_executable=unknown.executable,
        checked_at=datetime.now(timezone.utc),
    )

    assert perform_update(unknown, status).status.state is CliUpdateState.BLOCKED
    conflict_status = check_update(conflict, runner=lambda *_: CommandResult(returncode=1))
    assert conflict_status.state is CliUpdateState.BLOCKED
    active = _installation(tmp_path / "active")
    active_status = status.model_copy(
        update={
            "install_source": active.install_source,
            "active_executable": active.executable,
            "resolved_executable": active.executable,
        }
    )
    result = perform_update(active, active_status, active_run_ids=["run_live"])
    assert result.status.state is CliUpdateState.BLOCKED
    assert "run_live" in result.detail


def test_update_dry_run_and_exact_binary_verification(tmp_path: Path):
    installation = _installation(tmp_path)
    status = CliUpdateStatus(
        current_version="1.0.0",
        latest_version="1.2.0",
        update_available=True,
        state=CliUpdateState.AVAILABLE,
        install_source=installation.install_source,
        active_executable=installation.executable,
        resolved_executable=installation.executable,
    )
    calls: list[list[str]] = []

    def runner(argv, timeout, limit):
        del timeout, limit
        calls.append(list(argv))
        if argv == [installation.executable, "--version"]:
            return CommandResult(returncode=0, stdout="codex-cli 1.2.0")
        return CommandResult(returncode=0, stdout="updated")

    dry = perform_update(installation, status, dry_run=True, runner=runner)
    assert dry.ran is False
    assert calls == []

    result = perform_update(installation, status, runner=runner)
    assert result.ran is True
    assert calls[-1] == [installation.executable, "--version"]
    assert result.status.state is CliUpdateState.CURRENT


def test_package_manager_updates_never_invoke_sudo(tmp_path: Path):
    installation = _installation(tmp_path, cli_type="claude", source=CliInstallSource.APT)
    status = check_update(installation, runner=lambda *_: CommandResult(returncode=0))
    result = perform_update(installation, status)

    assert result.ran is False
    assert result.status.state is CliUpdateState.BLOCKED
    assert "never invokes sudo" in result.detail


def test_codex_native_check_probes_help_and_uses_official_release_metadata(tmp_path: Path):
    installation = _installation(tmp_path, source=CliInstallSource.STANDALONE_RELEASE)

    def runner(argv, timeout, limit):
        del timeout, limit
        if argv == [installation.executable, "--help"]:
            return CommandResult(returncode=0, stdout="Commands:\n  update  Update Codex\n")
        if argv == [installation.executable, "update", "--help"]:
            return CommandResult(returncode=0, stdout="Update the installed standalone CLI")
        raise AssertionError(argv)

    def fetcher(url, timeout, limit):
        del timeout, limit
        assert url.endswith("/openai/codex/releases/latest")
        return {"tag_name": "rust-v1.2.0"}

    status = check_update(installation, runner=runner, fetcher=fetcher)

    assert status.latest_version == "1.2.0"
    assert status.update_method == "codex-update"
    assert status.state is CliUpdateState.AVAILABLE


def test_antigravity_check_uses_official_github_release_api(tmp_path: Path):
    installation = _installation(tmp_path, cli_type="antigravity", source=CliInstallSource.NATIVE)

    def fetcher(url, timeout, limit):
        del timeout, limit
        assert "google-antigravity/antigravity-cli" in url
        return {"tag_name": "v1.4.0"}

    status = check_update(
        installation,
        runner=lambda *_: CommandResult(returncode=0),
        fetcher=fetcher,
    )

    assert status.latest_version == "1.4.0"
    assert status.update_method == "agy-official-installer"


def test_antigravity_official_installer_is_materialized_then_verified(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    installation = _installation(tmp_path, cli_type="antigravity", source=CliInstallSource.NATIVE)
    status = CliUpdateStatus(
        current_version="1.0.0",
        latest_version="1.2.0",
        update_available=True,
        state=CliUpdateState.AVAILABLE,
        install_source=installation.install_source,
        active_executable=installation.executable,
        resolved_executable=installation.executable,
        update_method="agy-official-installer",
    )
    calls: list[list[str]] = []

    def runner(argv, timeout, limit):
        del timeout, limit
        calls.append(list(argv))
        if argv[0] == "bash":
            assert Path(argv[1]).is_file()
            assert argv[-2:] == ["--skip-path", "--skip-aliases"]
            return CommandResult(returncode=0, stdout="installed")
        if argv == [installation.executable, "--version"]:
            return CommandResult(returncode=0, stdout="agy 1.2.0")
        if argv == [installation.executable, "models"]:
            return CommandResult(returncode=0, stdout="Model A")
        raise AssertionError(argv)

    result = perform_update(
        installation,
        status,
        runner=runner,
        installer_fetcher=lambda *_: b"#!/bin/sh\nexit 0\n",
    )

    assert result.ran is True
    assert result.status.state is CliUpdateState.CURRENT
    assert calls[-1] == [installation.executable, "models"]
    assert not Path(calls[0][1]).exists(), "private installer temp file was not cleaned up"
