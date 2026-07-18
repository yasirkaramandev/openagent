"""Source-aware CLI update checking and bounded, non-elevated update execution."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...core.models import (
    CliInstallation,
    CliInstallSource,
    CliUpdatePolicy,
    CliUpdateState,
    CliUpdateStatus,
)
from ...security.atomic import atomic_write_text
from ...security.process import minimal_environment
from .locator import CommandResult, CommandRunner, run_bounded

CHECK_TIMEOUT_SECONDS = 15
UPDATE_TIMEOUT_SECONDS = 180
MAX_UPDATE_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_HTTP_BODY_BYTES = 2 * 1024 * 1024

_NPM_PACKAGE = {"codex": "@openai/codex", "claude": "@anthropic-ai/claude-code"}


class CliUpdateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: CliUpdatePolicy = CliUpdatePolicy.ASK
    check_interval_hours: int = Field(default=6, ge=1, le=24 * 30)
    check_before_run: bool = True


def load_update_config(config_dir: Path) -> CliUpdateConfig:
    path = config_dir / "config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return CliUpdateConfig()
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return CliUpdateConfig()
    section = raw.get("cli_updates", {}) if isinstance(raw, dict) else {}
    try:
        return CliUpdateConfig.model_validate(section)
    except ValidationError:
        return CliUpdateConfig()


def save_update_config(config_dir: Path, config: CliUpdateConfig) -> None:
    path = config_dir / "config.json"
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError, ValueError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing["cli_updates"] = config.model_dump(mode="json")
    atomic_write_text(path, json.dumps(existing, indent=2), mode=0o600)


def update_environment(parent: Mapping[str, str] | None = None) -> dict[str, str]:
    """Minimal child environment plus only transport settings needed by package managers."""

    source = os.environ if parent is None else parent
    env = minimal_environment()
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "NODE_EXTRA_CA_CERTS",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    ):
        if key in source:
            env[key] = source[key]
    return env


def run_network_bounded(argv: Sequence[str], timeout: int, max_output_bytes: int) -> CommandResult:
    """Bounded package-manager process without leaking provider credentials."""

    return run_bounded(
        argv,
        timeout,
        max_output_bytes,
        env=update_environment(),
    )


JsonFetcher = Callable[[str, int, int], dict[str, Any]]
BytesFetcher = Callable[[str, int, int], bytes]


def fetch_json(url: str, timeout: int, max_body_bytes: int) -> dict[str, Any]:
    """Fetch one official JSON endpoint with a streaming body limit."""

    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > max_body_bytes:
                    raise ValueError(f"update metadata exceeds {max_body_bytes} bytes")
    value = json.loads(bytes(body))
    if not isinstance(value, dict):
        raise ValueError("update metadata is not a JSON object")
    return value


def fetch_bytes(url: str, timeout: int, max_body_bytes: int) -> bytes:
    """Fetch an official installer with a byte cap and a constrained redirect destination."""

    allowed_hosts = {
        "chatgpt.com",
        "antigravity.google",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            hostname = response.url.host.lower()
            if hostname not in allowed_hosts:
                raise ValueError(f"installer redirected to untrusted host {hostname!r}")
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > max_body_bytes:
                    raise ValueError(f"installer exceeds {max_body_bytes} bytes")
    if not body:
        raise ValueError("installer response was empty")
    return bytes(body)


def _version_tuple(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    match = re.search(r"\d+(?:\.\d+)+", value)
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


def _is_newer(latest: str | None, current: str | None) -> bool | None:
    left, right = _version_tuple(latest), _version_tuple(current)
    if left is None or right is None:
        return None
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))


def _json_command(
    runner: CommandRunner, argv: Sequence[str], *, timeout: int = CHECK_TIMEOUT_SECONDS
) -> dict[str, Any]:
    result = runner(list(argv), timeout, MAX_UPDATE_OUTPUT_BYTES)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or f"exit {result.returncode}")[:500])
    value = json.loads(result.stdout)
    if isinstance(value, str):
        return {"value": value}
    if not isinstance(value, dict):
        raise ValueError("command returned non-object JSON")
    return value


def _latest_npm(cli_type: str, runner: CommandRunner) -> str:
    package = _NPM_PACKAGE[cli_type]
    payload = _json_command(runner, ["npm", "view", package, "version", "--json"])
    value = payload.get("value")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("npm metadata omitted version")
    return value.strip()


def _latest_brew(cli_type: str, installation: CliInstallation, runner: CommandRunner) -> str:
    if cli_type == "claude":
        cask = "claude-code@latest" if installation.release_channel == "latest" else "claude-code"
    else:
        cask = "codex"
    payload = _json_command(runner, ["brew", "info", "--json=v2", "--cask", cask])
    casks = payload.get("casks")
    if not isinstance(casks, list) or not casks or not isinstance(casks[0], dict):
        raise ValueError("Homebrew metadata omitted cask")
    version = casks[0].get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("Homebrew metadata omitted version")
    return version


def _latest_winget(cli_type: str, runner: CommandRunner) -> str | None:
    package_id = "Anthropic.ClaudeCode" if cli_type == "claude" else "OpenAI.Codex"
    result = runner(
        ["winget", "list", "--id", package_id, "--exact", "--upgrade-available"],
        CHECK_TIMEOUT_SECONDS,
        MAX_UPDATE_OUTPUT_BYTES,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "winget check failed")[:500])
    versions = re.findall(r"\b\d+(?:\.\d+){1,3}\b", result.stdout)
    return versions[-1] if len(versions) >= 2 else None


def _latest_codex_release(fetcher: JsonFetcher) -> str:
    payload = fetcher(
        "https://api.github.com/repos/openai/codex/releases/latest",
        CHECK_TIMEOUT_SECONDS,
        MAX_HTTP_BODY_BYTES,
    )
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise ValueError("Codex release metadata omitted tag_name")
    return tag.removeprefix("rust-v").removeprefix("v")


def _latest_agy_release(fetcher: JsonFetcher) -> str:
    payload = fetcher(
        "https://api.github.com/repos/google-antigravity/antigravity-cli/releases/latest",
        CHECK_TIMEOUT_SECONDS,
        MAX_HTTP_BODY_BYTES,
    )
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise ValueError("Antigravity release metadata omitted tag_name")
    return tag.removeprefix("v")


def _codex_native_update_method(installation: CliInstallation, runner: CommandRunner) -> str:
    """Probe only documented help surfaces; never invoke an updater during a check."""

    result = runner(
        [installation.executable, "--help"],
        CHECK_TIMEOUT_SECONDS,
        MAX_UPDATE_OUTPUT_BYTES,
    )
    if result.returncode == 0:
        help_text = f"{result.stdout}\n{result.stderr}"
        if re.search(r"(?m)^\s*update(?:\s|$)", help_text):
            subcommand = runner(
                [installation.executable, "update", "--help"],
                CHECK_TIMEOUT_SECONDS,
                MAX_UPDATE_OUTPUT_BYTES,
            )
            if subcommand.returncode == 0:
                return "codex-update"
        if "--upgrade" in help_text:
            return "codex-upgrade"
    # The official installer is the source-matched fallback for a proven standalone install.
    return "codex-official-installer"


def _check_method_and_latest(
    installation: CliInstallation,
    runner: CommandRunner,
    fetcher: JsonFetcher,
) -> tuple[str, str | None, str | None]:
    source = installation.install_source
    if source is CliInstallSource.NPM and installation.type in _NPM_PACKAGE:
        return "npm-registry", _latest_npm(installation.type, runner), "npm-install-latest"
    if source is CliInstallSource.HOMEBREW_CASK:
        return (
            "homebrew-json",
            _latest_brew(installation.type, installation, runner),
            "brew-upgrade-cask",
        )
    if source is CliInstallSource.WINGET:
        return "winget-list", _latest_winget(installation.type, runner), "winget-upgrade"
    if source is CliInstallSource.HOMEBREW_FORMULA_LEGACY:
        return "homebrew-legacy", None, None
    if installation.type == "codex" and source in {
        CliInstallSource.NATIVE,
        CliInstallSource.STANDALONE_RELEASE,
    }:
        return (
            "github-releases-api",
            _latest_codex_release(fetcher),
            _codex_native_update_method(installation, runner),
        )
    if installation.type == "claude" and source is CliInstallSource.NATIVE:
        # Claude's public CLI has a manual updater but no stable check-only command. Do not guess.
        return "native-updater", None, "claude-update"
    if installation.type == "antigravity" and source is CliInstallSource.NATIVE:
        return (
            "github-releases-api",
            _latest_agy_release(fetcher),
            "agy-official-installer",
        )
    if source in {CliInstallSource.APT, CliInstallSource.DNF, CliInstallSource.APK}:
        return f"{source.value}-metadata", None, None
    return "unavailable", None, None


def check_update(
    installation: CliInstallation,
    *,
    runner: CommandRunner = run_network_bounded,
    fetcher: JsonFetcher = fetch_json,
    cache_hours: int = 6,
    now: datetime | None = None,
) -> CliUpdateStatus:
    now = now or datetime.now(timezone.utc)
    expires = now + timedelta(hours=cache_hours)
    base = CliUpdateStatus(
        current_version=installation.version,
        install_source=installation.install_source,
        active_executable=installation.executable,
        resolved_executable=installation.resolved_executable or installation.executable,
        shadowed_executables=list(installation.shadowed_executables),
        checked_at=now,
        cache_expires_at=expires,
    )
    if installation.shadowed_executables:
        return base.model_copy(
            update={
                "state": CliUpdateState.BLOCKED,
                "check_method": "conflict-check",
                "detail": "multiple independent installations detected; automatic update is blocked",
            }
        )
    try:
        method, latest, update_method = _check_method_and_latest(installation, runner, fetcher)
        available = _is_newer(latest, installation.version)
        if latest is None:
            state = CliUpdateState.UNKNOWN
            detail = (
                "installed version detected; this install source has no stable check-only surface"
            )
        elif available is True:
            state = CliUpdateState.AVAILABLE
            detail = f"{installation.version or 'unknown'} -> {latest}"
        elif available is False:
            state = CliUpdateState.CURRENT
            detail = f"current ({installation.version or latest})"
        else:
            state = CliUpdateState.UNKNOWN
            detail = "version metadata could not be compared safely"
        if installation.install_source is CliInstallSource.HOMEBREW_FORMULA_LEGACY:
            state = CliUpdateState.BLOCKED
            detail = (
                "legacy Homebrew formula requires an explicit migration; no automatic uninstall"
            )
        if installation.install_source is CliInstallSource.UNKNOWN:
            state = CliUpdateState.BLOCKED
            detail = "installation source is unknown; automatic update is blocked"
        return base.model_copy(
            update={
                "latest_version": latest,
                "update_available": available,
                "state": state,
                "check_method": method,
                "update_method": update_method,
                "detail": detail,
            }
        )
    except Exception as exc:
        return base.model_copy(
            update={
                "state": CliUpdateState.CHECK_FAILED,
                "check_method": "source-metadata",
                "detail": f"update check failed: {str(exc)[:500]}",
            }
        )


def cache_valid(status: CliUpdateStatus | None, *, now: datetime | None = None) -> bool:
    if status is None or status.cache_expires_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    expires = status.cache_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > current


class UpdateExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: CliUpdateStatus
    command: list[str] | None = None
    ran: bool = False
    detail: str = ""


def _installer_placeholder(cli_type: str) -> list[str]:
    if sys.platform.startswith("win"):
        return [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            f"<official-{cli_type}-installer.ps1>",
        ]
    args = ["bash", f"<official-{cli_type}-installer.sh>"]
    if cli_type == "antigravity":
        args.extend(["--skip-path", "--skip-aliases"])
    return args


def _update_argv(installation: CliInstallation, status: CliUpdateStatus) -> list[str] | None:
    source = installation.install_source
    cli_type = installation.type
    if source is CliInstallSource.NPM and cli_type in _NPM_PACKAGE:
        return ["npm", "install", "-g", f"{_NPM_PACKAGE[cli_type]}@latest"]
    if source is CliInstallSource.HOMEBREW_CASK:
        if cli_type == "claude":
            cask = (
                "claude-code@latest" if installation.release_channel == "latest" else "claude-code"
            )
        else:
            cask = "codex"
        return ["brew", "upgrade", "--cask", cask]
    if source is CliInstallSource.WINGET:
        package_id = "Anthropic.ClaudeCode" if cli_type == "claude" else "OpenAI.Codex"
        return ["winget", "upgrade", "--id", package_id, "--exact"]
    if source is CliInstallSource.NATIVE and cli_type == "claude":
        return [installation.executable, "update"]
    if cli_type == "codex" and source in {
        CliInstallSource.NATIVE,
        CliInstallSource.STANDALONE_RELEASE,
    }:
        if status.update_method == "codex-update":
            return [installation.executable, "update"]
        if status.update_method == "codex-upgrade":
            return [installation.executable, "--upgrade"]
        if status.update_method == "codex-official-installer":
            return _installer_placeholder("codex")
    if source is CliInstallSource.NATIVE and cli_type == "antigravity":
        return _installer_placeholder("antigravity")
    return None


def _materialize_official_installer(
    argv: list[str],
    cli_type: str,
    directory: Path,
    fetcher: BytesFetcher,
) -> list[str]:
    windows = sys.platform.startswith("win")
    suffix = ".ps1" if windows else ".sh"
    base = (
        "https://chatgpt.com/codex/install"
        if cli_type == "codex"
        else "https://antigravity.google/cli/install"
    )
    url = base + suffix
    payload = fetcher(url, CHECK_TIMEOUT_SECONDS, MAX_HTTP_BODY_BYTES)
    if windows:
        sample = payload[:4096].decode("utf-8", errors="replace").lower()
        if "powershell" not in sample and "param(" not in sample:
            raise ValueError("official installer response is not a PowerShell script")
    elif not payload.lstrip().startswith(b"#!"):
        raise ValueError("official installer response has no script shebang")
    target = directory / f"official-{cli_type}-installer{suffix}"
    # The private temporary directory and 0600/0700 mode prevent another local user from swapping
    # the downloaded script between validation and execution.
    target.write_bytes(payload)
    target.chmod(0o700)
    placeholder = f"<official-{cli_type}-installer{suffix}>"
    return [str(target) if argument == placeholder else argument for argument in argv]


def perform_update(
    installation: CliInstallation,
    status: CliUpdateStatus,
    *,
    active_run_ids: Sequence[str] = (),
    dry_run: bool = False,
    runner: CommandRunner = run_network_bounded,
    installer_fetcher: BytesFetcher = fetch_bytes,
) -> UpdateExecutionResult:
    """Execute only a source-matched, non-elevated update and verify the exact active binary."""

    def blocked(detail: str) -> UpdateExecutionResult:
        revised = status.model_copy(update={"state": CliUpdateState.BLOCKED, "detail": detail})
        return UpdateExecutionResult(status=revised, detail=detail)

    if active_run_ids:
        return blocked(f"CLI is used by active run(s): {', '.join(active_run_ids[:5])}")
    if installation.shadowed_executables:
        return blocked("multiple installations detected; select/remove conflicts before updating")
    if installation.install_source is CliInstallSource.UNKNOWN:
        return blocked("installation source is unknown; refusing automatic update")
    if os.environ.get("DISABLE_UPDATES") and installation.type == "claude":
        return blocked("Claude Code DISABLE_UPDATES is set")
    if (
        os.environ.get("DISABLE_AUTOUPDATER")
        and installation.type == "claude"
        and installation.install_source is CliInstallSource.NATIVE
    ):
        return blocked("Claude Code DISABLE_AUTOUPDATER is set for this native installation")
    if (
        os.environ.get("AGY_CLI_DISABLE_AUTO_UPDATE", "").lower() == "true"
        and installation.type == "antigravity"
    ):
        return blocked("Antigravity auto-update is disabled by AGY_CLI_DISABLE_AUTO_UPDATE")
    if (
        installation.type == "antigravity"
        and installation.install_source is CliInstallSource.NATIVE
    ):
        updater_lock = Path.home() / ".gemini" / "antigravity-cli" / "updater" / "update.lock"
        if updater_lock.exists():
            return blocked(
                f"Antigravity updater lock is present at {updater_lock}; OpenAgent will not remove it"
            )
    argv = _update_argv(installation, status)
    if argv is None:
        if installation.install_source in {
            CliInstallSource.APT,
            CliInstallSource.DNF,
            CliInstallSource.APK,
        }:
            return blocked(
                f"{installation.install_source.value} update requires administrator authority; "
                "OpenAgent never invokes sudo"
            )
        return blocked("no safe source-matched updater is available for this installation")
    target = Path(installation.resolved_executable or installation.executable)
    if not os.access(target, os.W_OK) and not os.access(target.parent, os.W_OK):
        return blocked("active executable and parent directory are not writable")
    if dry_run:
        return UpdateExecutionResult(
            status=status, command=argv, detail="dry-run; no update executed"
        )
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        effective_argv = argv
        if any(argument.startswith("<official-") for argument in argv):
            temporary = tempfile.TemporaryDirectory(prefix="openagent-cli-installer-")
            effective_argv = _materialize_official_installer(
                argv,
                installation.type,
                Path(temporary.name),
                installer_fetcher,
            )
        result = runner(effective_argv, UPDATE_TIMEOUT_SECONDS, MAX_UPDATE_OUTPUT_BYTES)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or f"exit {result.returncode}")[:500]
            failed = status.model_copy(
                update={"state": CliUpdateState.CHECK_FAILED, "detail": f"update failed: {detail}"}
            )
            return UpdateExecutionResult(
                status=failed, command=argv, ran=True, detail=failed.detail
            )
        verified = runner(
            [installation.executable, "--version"],
            CHECK_TIMEOUT_SECONDS,
            64 * 1024,
        )
        version = (verified.stdout or verified.stderr).strip().splitlines()[0]
        if verified.returncode != 0 or not version:
            raise RuntimeError("updated executable failed --version verification")
        if installation.type == "antigravity":
            models = runner(
                [installation.executable, "models"],
                CHECK_TIMEOUT_SECONDS,
                MAX_UPDATE_OUTPUT_BYTES,
            )
            if models.returncode != 0:
                raise RuntimeError(
                    "updated Antigravity executable failed authenticated model-surface verification"
                )
        available = _is_newer(status.latest_version, version)
        state = CliUpdateState.CURRENT if available is False else CliUpdateState.UNKNOWN
        detail = f"updated and verified exact executable: {version}"
        if available is True:
            detail += (
                f"; expected latest {status.latest_version}, but active version is still older"
            )
        revised = status.model_copy(
            update={
                "current_version": version,
                "update_available": available,
                "state": state,
                "detail": detail,
            }
        )
        return UpdateExecutionResult(status=revised, command=argv, ran=True, detail=detail)
    except PermissionError as exc:
        if sys.platform.startswith("win"):
            revised = status.model_copy(
                update={
                    "state": CliUpdateState.RESTART_REQUIRED,
                    "restart_required": True,
                    "detail": "Windows locked the active executable; restart required",
                }
            )
            return UpdateExecutionResult(
                status=revised, command=argv, ran=True, detail=revised.detail
            )
        return blocked(f"update permission denied: {exc}")
    except Exception as exc:
        failed = status.model_copy(
            update={
                "state": CliUpdateState.CHECK_FAILED,
                "detail": f"update failed: {str(exc)[:500]}",
            }
        )
        return UpdateExecutionResult(status=failed, command=argv, ran=True, detail=failed.detail)
    finally:
        if temporary is not None:
            temporary.cleanup()
