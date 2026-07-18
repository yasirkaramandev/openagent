"""CLI installation inspection and install-source provenance classification."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TypedDict

from ...core.models import CliInstallation, CliInstallSource
from .locator import CliLocation, CommandRunner, ExecutableCandidate, run_bounded

_NPM_PACKAGES = {
    "codex": "@openai/codex",
    "claude": "@anthropic-ai/claude-code",
}


class ClaudeUpdatePreferences(TypedDict):
    release_channel: str | None
    minimum_version: str | None
    auto_updates_disabled: bool
    package_manager_auto_update: bool | None


class AntigravityUpdaterState(TypedDict):
    updater_lock_path: str
    updater_lock_present: bool
    auto_updates_disabled: bool


def _metadata_succeeds(runner: CommandRunner, argv: list[str]) -> bool:
    try:
        return runner(argv, 8, 256 * 1024).returncode == 0
    except Exception:
        return False


def _npm_owns(cli_type: str, candidate: ExecutableCandidate, runner: CommandRunner) -> bool:
    package = _NPM_PACKAGES.get(cli_type)
    if not package:
        return False
    try:
        result = runner(["npm", "-g", "ls", package, "--json", "--depth=0"], 10, 512 * 1024)
        payload = json.loads(result.stdout or "{}")
    except Exception:
        return False
    dependencies = payload.get("dependencies") if isinstance(payload, dict) else None
    if result.returncode != 0 or not isinstance(dependencies, dict) or package not in dependencies:
        return False
    try:
        prefix = runner(["npm", "prefix", "-g"], 5, 64 * 1024).stdout.strip()
    except Exception:
        prefix = ""
    paths = f"{candidate.path}\n{candidate.resolved_path or ''}"
    return not prefix or os.path.normcase(prefix) in os.path.normcase(paths)


def detect_install_source(
    cli_type: str,
    candidate: ExecutableCandidate,
    *,
    runner: CommandRunner = run_bounded,
    platform: str | None = None,
) -> CliInstallSource:
    """Determine provenance from realpath plus package-manager evidence; never from symlink alone."""

    platform = sys.platform if platform is None else platform
    raw = f"{candidate.path}\n{candidate.resolved_path or ''}".lower().replace("\\", "/")
    origin = candidate.origin
    if origin == "winget" or "/microsoft/winget/" in raw:
        return CliInstallSource.WINGET
    if "/caskroom/" in raw:
        return CliInstallSource.HOMEBREW_CASK
    if "/cellar/" in raw:
        return CliInstallSource.HOMEBREW_FORMULA_LEGACY
    if origin == "npm" or "/node_modules/" in raw or "/appdata/roaming/npm/" in raw:
        return CliInstallSource.NPM
    if cli_type == "claude" and "/.claude/local/" in raw:
        return CliInstallSource.LEGACY_LOCAL
    if cli_type == "claude" and (
        "/.local/share/claude/releases/" in raw or "/.local/bin/claude" in raw
    ):
        # npm may also link a native payload into place, so package metadata wins when it owns this
        # exact prefix; otherwise this is the official standalone/native updater layout.
        if _npm_owns(cli_type, candidate, runner):
            return CliInstallSource.NPM
        return CliInstallSource.NATIVE
    if origin == "homebrew":
        cask = "codex" if cli_type == "codex" else "claude-code"
        if _metadata_succeeds(runner, ["brew", "list", "--cask", cask]):
            return CliInstallSource.HOMEBREW_CASK
        if cli_type == "codex" and _metadata_succeeds(runner, ["brew", "list", "codex"]):
            return CliInstallSource.HOMEBREW_FORMULA_LEGACY
    if cli_type in _NPM_PACKAGES and _npm_owns(cli_type, candidate, runner):
        return CliInstallSource.NPM
    if cli_type == "claude" and not platform.startswith(("darwin", "win")):
        if _metadata_succeeds(runner, ["dpkg-query", "-W", "claude-code"]):
            return CliInstallSource.APT
        if _metadata_succeeds(runner, ["rpm", "-q", "claude-code"]):
            return CliInstallSource.DNF
        if _metadata_succeeds(runner, ["apk", "info", "-e", "claude-code"]):
            return CliInstallSource.APK
    if origin == "legacy-local":
        return CliInstallSource.LEGACY_LOCAL
    if origin == "native":
        return (
            CliInstallSource.NATIVE
            if cli_type in {"claude", "antigravity"}
            else CliInstallSource.STANDALONE_RELEASE
        )
    if cli_type == "codex" and any(marker in raw for marker in ("/.local/bin/", "/releases/")):
        return CliInstallSource.STANDALONE_RELEASE
    if cli_type == "antigravity" and ("/.local/bin/agy" in raw or "/agy/bin/agy" in raw):
        return CliInstallSource.NATIVE
    return CliInstallSource.UNKNOWN


def inspect_installation(
    cli_type: str,
    location: CliLocation,
    *,
    adapter: str,
    validated_version: str | None = None,
    experimental: bool = False,
    runner: CommandRunner = run_bounded,
    platform: str | None = None,
    release_channel: str | None = None,
    minimum_version: str | None = None,
    auto_updates_disabled: bool = False,
    package_manager_auto_update: bool | None = None,
    updater_lock_path: str | None = None,
    updater_lock_present: bool = False,
) -> CliInstallation | None:
    active = next((candidate for candidate in location.candidates if candidate.active), None)
    if active is None or not active.valid:
        return None
    source = detect_install_source(cli_type, active, runner=runner, platform=platform)
    return CliInstallation(
        id=f"cli_{cli_type}",
        type=cli_type,
        executable=active.path,
        resolved_executable=active.resolved_path,
        shadowed_executables=list(location.shadowed_executables),
        install_source=source,
        version=active.version,
        adapter=adapter,
        authenticated=None,
        experimental=experimental,
        validated_version=validated_version,
        release_channel=release_channel,
        minimum_version=minimum_version,
        auto_updates_disabled=auto_updates_disabled,
        package_manager_auto_update=package_manager_auto_update,
        updater_lock_path=updater_lock_path,
        updater_lock_present=updater_lock_present,
    )


def claude_update_preferences(
    *, home: Path | None = None, env: dict[str, str] | None = None
) -> ClaudeUpdatePreferences:
    """Read documented updater policy without touching Claude credential files."""

    home = Path.home() if home is None else home
    env = dict(os.environ if env is None else env)
    settings: dict[str, object] = {}
    path = home / ".claude" / "settings.json"
    try:
        if path.stat().st_size <= 1024 * 1024:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                settings = value
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    package_auto = env.get("CLAUDE_CODE_PACKAGE_MANAGER_AUTO_UPDATE")
    return {
        "release_channel": (
            str(settings["autoUpdatesChannel"])
            if settings.get("autoUpdatesChannel") in {"stable", "latest"}
            else None
        ),
        "minimum_version": (
            str(settings["minimumVersion"])
            if isinstance(settings.get("minimumVersion"), str)
            else None
        ),
        "auto_updates_disabled": bool(env.get("DISABLE_AUTOUPDATER") or env.get("DISABLE_UPDATES")),
        "package_manager_auto_update": (
            package_auto.strip().lower() in {"1", "true", "yes", "on"}
            if package_auto is not None
            else None
        ),
    }


def antigravity_updater_state(*, home: Path | None = None) -> AntigravityUpdaterState:
    home = Path.home() if home is None else home
    lock = home / ".gemini" / "antigravity-cli" / "updater" / "update.lock"
    return {
        "updater_lock_path": str(lock),
        "updater_lock_present": lock.exists(),
        "auto_updates_disabled": os.environ.get("AGY_CLI_DISABLE_AUTO_UPDATE", "").lower()
        == "true",
    }


def active_candidate(location: CliLocation) -> ExecutableCandidate | None:
    return next((candidate for candidate in location.candidates if candidate.active), None)


def installation_path(installation: CliInstallation) -> Path:
    return Path(installation.resolved_executable or installation.executable)
