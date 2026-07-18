"""Source-aware, fail-closed OpenAgent self updates.

This module deliberately does not construct :class:`~openagent.app.OpenAgentApp`.  A user must be
able to repair OpenAgent with ``openagent update`` even when opening the application database would
fail because its schema is newer, a record is corrupt, or a migration was interrupted.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from .. import __version__
from ..credentials.redaction import redact
from ..runtimes.cli.locator import CommandResult, run_bounded
from ..runtimes.cli.updates import (
    CHECK_TIMEOUT_SECONDS,
    MAX_HTTP_BODY_BYTES,
    MAX_UPDATE_OUTPUT_BYTES,
    UPDATE_TIMEOUT_SECONDS,
    JsonFetcher,
    fetch_json,
    update_environment,
)

OFFICIAL_REPOSITORY = "yasirkaramandev/openagent"
PYPI_METADATA_URL = "https://pypi.org/pypi/openagent/json"

SelfUpdateSource = Literal["source-checkout", "uv-tool", "pipx", "pip", "unsupported"]


class SelfUpdatePlan(BaseModel):
    """A fully resolved update plan safe to show before any mutation."""

    model_config = ConfigDict(extra="forbid")

    current_version: str
    latest_version: str | None = None
    source: SelfUpdateSource
    active_executable: str
    resolved_executable: str
    check_method: str
    update_available: bool | None = None
    can_update: bool = False
    commands: list[list[str]] = Field(default_factory=list)
    checkout_root: str | None = None
    local_revision: str | None = None
    remote_revision: str | None = None
    detail: str = ""


class SelfUpdateResult(BaseModel):
    """Outcome of an update plus its post-install health verification."""

    model_config = ConfigDict(extra="forbid")

    plan: SelfUpdatePlan
    ok: bool
    ran: bool = False
    verified_version: str | None = None
    doctor_exit_code: int | None = None
    backup_path: str | None = None
    error_type: str | None = None
    detail: str = ""


SelfUpdateRunner = Callable[
    [Sequence[str], int, int, Mapping[str, str], Path | None], CommandResult
]
ExecutableResolver = Callable[[str], str | None]


def run_self_update_command(
    argv: Sequence[str],
    timeout: int,
    max_output_bytes: int,
    env: Mapping[str, str],
    cwd: Path | None,
) -> CommandResult:
    """Run an updater with bounded output and the credential-free network environment."""

    return run_bounded(
        argv,
        timeout,
        max_output_bytes,
        env=env,
        cwd=cwd,
    )


def _active_executable(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().absolute()
    invoked = Path(sys.argv[0]).expanduser()
    if invoked.name.lower().startswith("openagent") and invoked.exists():
        return invoked.absolute()
    located = shutil.which("openagent")
    if located:
        return Path(located).absolute()
    # This occurs for ``python -m openagent``. The interpreter is the only exact executable we can
    # prove, but it cannot be used as a console-script verifier, so the resulting plan is blocked.
    return Path(sys.executable).absolute()


def _resolved(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        return path.resolve(strict=False)


def _direct_url_payload() -> dict[str, Any] | None:
    try:
        raw = importlib.metadata.distribution("openagent").read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return None
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"invalid": True}
    return value if isinstance(value, dict) else {"invalid": True}


def _run(
    runner: SelfUpdateRunner,
    argv: Sequence[str],
    *,
    timeout: int = CHECK_TIMEOUT_SECONDS,
    limit: int = MAX_UPDATE_OUTPUT_BYTES,
    cwd: Path | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> CommandResult:
    env = update_environment()
    if extra_env:
        env.update(extra_env)
    try:
        return runner(argv, timeout, limit, env, cwd)
    except (OSError, RuntimeError) as exc:
        return CommandResult(returncode=127, stderr=exc.__class__.__name__)


def _command_text(result: CommandResult) -> str:
    text = redact(result.stderr or result.stdout or f"exit {result.returncode}")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (lines[-1] if lines else f"exit {result.returncode}")[:500]


def _one_line(result: CommandResult) -> str | None:
    for line in (result.stdout or result.stderr).splitlines():
        if line.strip():
            return line.strip()[:500]
    return None


def _version(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)(?!\d)", value)
    return match.group(1) if match else None


def _version_tuple(value: str | None) -> tuple[int, int, int] | None:
    parsed = _version(value)
    if parsed is None:
        return None
    base = parsed.split("-", 1)[0].split("+", 1)[0]
    major, minor, patch = base.split(".")
    return int(major), int(minor), int(patch)


def _compare(latest: str, current: str) -> bool | None:
    left, right = _version_tuple(latest), _version_tuple(current)
    if left is None or right is None:
        return None
    return left > right


def _official_origin(value: str) -> bool:
    normalized = value.strip().lower().replace("\\", "/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    accepted = {
        f"https://github.com/{OFFICIAL_REPOSITORY}",
        f"ssh://git@github.com/{OFFICIAL_REPOSITORY}",
        f"git@github.com:{OFFICIAL_REPOSITORY}",
    }
    return normalized in accepted


def _file_checkout(payload: Mapping[str, Any] | None) -> Path | None:
    if not payload or not isinstance(payload.get("dir_info"), dict):
        return None
    raw = payload.get("url")
    if not isinstance(raw, str):
        return None
    parsed = urlsplit(raw)
    if parsed.scheme not in {"", "file"} or parsed.netloc not in {"", "localhost"}:
        return None
    value = unquote(parsed.path if parsed.scheme == "file" else raw)
    return Path(value).expanduser().resolve(strict=False) if value else None


def _blocked_plan(
    *,
    current_version: str,
    active: Path,
    source: SelfUpdateSource,
    method: str,
    detail: str,
    checkout_root: Path | None = None,
) -> SelfUpdatePlan:
    return SelfUpdatePlan(
        current_version=current_version,
        source=source,
        active_executable=str(active),
        resolved_executable=str(_resolved(active)),
        check_method=method,
        can_update=False,
        checkout_root=str(checkout_root) if checkout_root is not None else None,
        detail=detail,
    )


def _git_value(
    runner: SelfUpdateRunner, root: Path, argv: Sequence[str]
) -> tuple[str | None, CommandResult]:
    result = _run(runner, ["git", "-C", str(root), *argv], cwd=root)
    return _one_line(result) if result.returncode == 0 else None, result


def _source_plan(
    *,
    root: Path,
    current_version: str,
    active: Path,
    runner: SelfUpdateRunner,
    platform: str,
) -> SelfUpdatePlan:
    setup_name = "setup.ps1" if platform.startswith("win") else "setup.sh"
    setup = root / setup_name
    if not root.is_dir() or not setup.is_file():
        return _blocked_plan(
            current_version=current_version,
            active=active,
            source="source-checkout",
            method="git-origin-main",
            checkout_root=root,
            detail=f"local source checkout is missing {setup_name}",
        )

    top, result = _git_value(runner, root, ["rev-parse", "--show-toplevel"])
    if result.returncode != 0 or top is None or Path(top).resolve(strict=False) != root:
        return _blocked_plan(
            current_version=current_version,
            active=active,
            source="source-checkout",
            method="git-origin-main",
            checkout_root=root,
            detail="local install source is not the root of a Git checkout",
        )
    origin, result = _git_value(runner, root, ["remote", "get-url", "origin"])
    if result.returncode != 0 or origin is None or not _official_origin(origin):
        return _blocked_plan(
            current_version=current_version,
            active=active,
            source="source-checkout",
            method="git-origin-main",
            checkout_root=root,
            detail="source checkout origin is not the official OpenAgent repository",
        )
    branch, result = _git_value(runner, root, ["branch", "--show-current"])
    if result.returncode != 0 or branch != "main":
        return _blocked_plan(
            current_version=current_version,
            active=active,
            source="source-checkout",
            method="git-origin-main",
            checkout_root=root,
            detail="source checkout must be on branch main before automatic update",
        )
    status = _run(
        runner,
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
        cwd=root,
    )
    if status.returncode != 0 or status.stdout.strip():
        return _blocked_plan(
            current_version=current_version,
            active=active,
            source="source-checkout",
            method="git-origin-main",
            checkout_root=root,
            detail="source checkout has local changes; automatic update is blocked",
        )
    local, local_result = _git_value(runner, root, ["rev-parse", "HEAD"])
    remote_result = _run(
        runner,
        ["git", "-C", str(root), "ls-remote", "--heads", "origin", "main"],
        cwd=root,
    )
    remote_line = _one_line(remote_result)
    remote = remote_line.split()[0] if remote_line else None
    if local_result.returncode != 0 or local is None or remote_result.returncode != 0 or not remote:
        return _blocked_plan(
            current_version=current_version,
            active=active,
            source="source-checkout",
            method="git-origin-main",
            checkout_root=root,
            detail="could not verify the official origin/main revision",
        )

    if platform.startswith("win"):
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(setup),
        ]
    else:
        command = ["sh", str(setup)]
    available = local != remote
    return SelfUpdatePlan(
        current_version=current_version,
        source="source-checkout",
        active_executable=str(active),
        resolved_executable=str(_resolved(active)),
        check_method="git-origin-main",
        update_available=available,
        can_update=True,
        commands=[
            ["git", "-C", str(root), "pull", "--ff-only", "origin", "main"],
            command,
        ],
        checkout_root=str(root),
        local_revision=local,
        remote_revision=remote,
        detail=(
            f"official main has a newer revision ({local[:12]} -> {remote[:12]})"
            if available
            else f"current at official main revision {local[:12]}"
        ),
    )


def _manager_source(
    *,
    prefix: Path,
    runner: SelfUpdateRunner,
    environ: Mapping[str, str],
) -> SelfUpdateSource:
    uv = shutil.which("uv", path=environ.get("PATH"))
    if uv:
        result = _run(runner, [uv, "tool", "dir"])
        line = _one_line(result)
        if result.returncode == 0 and line:
            tool_root = Path(line).expanduser().resolve(strict=False)
            if prefix.resolve(strict=False).parent == tool_root:
                return "uv-tool"

    normalized = str(prefix).replace("\\", "/").lower()
    pipx_home = environ.get("PIPX_HOME")
    if "/pipx/venvs/openagent" in normalized or (
        pipx_home
        and prefix.resolve(strict=False).parent
        == (Path(pipx_home).expanduser().resolve(strict=False) / "venvs")
    ):
        return "pipx"
    return "pip"


def _pypi_plan(
    *,
    source: SelfUpdateSource,
    current_version: str,
    active: Path,
    prefix: Path,
    executable: Path,
    fetcher: JsonFetcher,
    environ: Mapping[str, str],
) -> SelfUpdatePlan:
    try:
        payload = fetcher(PYPI_METADATA_URL, CHECK_TIMEOUT_SECONDS, MAX_HTTP_BODY_BYTES)
        info = payload.get("info")
        latest = info.get("version") if isinstance(info, dict) else None
        if not isinstance(latest, str) or _version(latest) is None:
            raise ValueError("PyPI metadata omitted a valid version")
    except Exception as exc:
        return _blocked_plan(
            current_version=current_version,
            active=active,
            source=source,
            method="pypi-json",
            detail=f"official PyPI update check failed: {exc.__class__.__name__}",
        )

    available = _compare(latest, current_version)
    if available is None:
        return _blocked_plan(
            current_version=current_version,
            active=active,
            source=source,
            method="pypi-json",
            detail="installed and latest versions cannot be compared safely",
        ).model_copy(update={"latest_version": latest})

    if source == "uv-tool":
        uv = shutil.which("uv", path=environ.get("PATH"))
        if not uv:
            return _blocked_plan(
                current_version=current_version,
                active=active,
                source=source,
                method="pypi-json",
                detail="this is a uv tool install, but uv is not available",
            ).model_copy(update={"latest_version": latest, "update_available": available})
        command = [uv, "tool", "upgrade", "openagent"]
    elif source == "pipx":
        pipx = shutil.which("pipx", path=environ.get("PATH"))
        if not pipx:
            return _blocked_plan(
                current_version=current_version,
                active=active,
                source=source,
                method="pypi-json",
                detail="this is a pipx install, but pipx is not available",
            ).model_copy(update={"latest_version": latest, "update_available": available})
        command = [pipx, "upgrade", "openagent"]
    else:
        # Use the interpreter belonging to the active distribution, never a different ``pip`` from
        # PATH. PEP 668 or permissions may reject this; that is a safe, honest failure.
        command = [str(executable), "-m", "pip", "install", "--upgrade", "openagent"]

    return SelfUpdatePlan(
        current_version=current_version,
        latest_version=latest,
        source=source,
        active_executable=str(active),
        resolved_executable=str(_resolved(active)),
        check_method="pypi-json",
        update_available=available,
        can_update=True,
        commands=[command],
        detail=(f"{current_version} -> {latest}" if available else f"current ({current_version})"),
    )


def check_self_update(
    *,
    current_version: str = __version__,
    active_executable: str | None = None,
    python_executable: str | None = None,
    prefix: str | None = None,
    direct_url: Mapping[str, Any] | None | Literal[False] = False,
    runner: SelfUpdateRunner = run_self_update_command,
    fetcher: JsonFetcher = fetch_json,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> SelfUpdatePlan:
    """Resolve provenance and check only the matching official update source.

    ``direct_url=False`` means "read installed metadata"; tests can pass ``None`` to explicitly
    model an index-installed wheel.
    """

    active = _active_executable(active_executable)
    if active.name.lower() in {"python", "python.exe"}:
        return _blocked_plan(
            current_version=current_version,
            active=active,
            source="unsupported",
            method="entrypoint",
            detail="invoke the installed `openagent` command directly to update it",
        )
    payload = _direct_url_payload() if direct_url is False else direct_url
    checkout = _file_checkout(payload)
    if checkout is not None:
        return _source_plan(
            root=checkout,
            current_version=current_version,
            active=active,
            runner=runner,
            platform=sys.platform if platform is None else platform,
        )
    if payload is not None:
        return _blocked_plan(
            current_version=current_version,
            active=active,
            source="unsupported",
            method="direct-url",
            detail="remote or malformed direct-URL installs cannot be updated safely in place",
        )

    environment = dict(os.environ if environ is None else environ)
    runtime_prefix = Path(sys.prefix if prefix is None else prefix)
    runtime_executable = Path(sys.executable if python_executable is None else python_executable)
    source = _manager_source(prefix=runtime_prefix, runner=runner, environ=environment)
    return _pypi_plan(
        source=source,
        current_version=current_version,
        active=active,
        prefix=runtime_prefix,
        executable=runtime_executable,
        fetcher=fetcher,
        environ=environment,
    )


def _source_version(root: Path) -> str | None:
    try:
        text = (root / "src" / "openagent" / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'(?m)^__version__\s*=\s*["\']([^"\']+)["\']\s*$', text)
    return match.group(1) if match else None


def _backup_path(payload: Any) -> str | None:
    if isinstance(payload, dict):
        value = payload.get("backup_path")
        if isinstance(value, str) and value:
            return value
        for child in payload.values():
            found = _backup_path(child)
            if found:
                return found
    elif isinstance(payload, list):
        for child in payload:
            found = _backup_path(child)
            if found:
                return found
    return None


def perform_self_update(
    plan: SelfUpdatePlan,
    *,
    runner: SelfUpdateRunner = run_self_update_command,
    resolver: ExecutableResolver = shutil.which,
) -> SelfUpdateResult:
    """Execute ``plan`` and verify revision, exact binary, version, PATH, and Doctor health."""

    if not plan.can_update:
        return SelfUpdateResult(
            plan=plan,
            ok=False,
            error_type="update_blocked",
            detail=plan.detail,
        )
    if plan.update_available is False:
        return SelfUpdateResult(plan=plan, ok=True, detail=plan.detail)

    root = Path(plan.checkout_root) if plan.checkout_root else None
    for command in plan.commands:
        result = _run(
            runner,
            command,
            timeout=UPDATE_TIMEOUT_SECONDS,
            cwd=root,
            extra_env={"OPENAGENT_SETUP_NO_LAUNCH": "1"},
        )
        if result.returncode != 0:
            return SelfUpdateResult(
                plan=plan,
                ok=False,
                ran=True,
                error_type="update_command_failed",
                detail=f"update command failed: {_command_text(result)}",
            )

    if root is not None and plan.remote_revision:
        head, result = _git_value(runner, root, ["rev-parse", "HEAD"])
        if result.returncode != 0 or head != plan.remote_revision:
            return SelfUpdateResult(
                plan=plan,
                ok=False,
                ran=True,
                error_type="revision_verification_failed",
                detail="source checkout did not reach the verified origin/main revision",
            )

    expected = plan.latest_version
    if root is not None:
        expected = _source_version(root)
        if expected is None:
            return SelfUpdateResult(
                plan=plan,
                ok=False,
                ran=True,
                error_type="version_verification_failed",
                detail="updated source does not declare a valid OpenAgent version",
            )

    active = Path(plan.active_executable)
    resolved_by_name = resolver("openagent")
    if resolved_by_name is None or _resolved(Path(resolved_by_name)) != _resolved(active):
        return SelfUpdateResult(
            plan=plan,
            ok=False,
            ran=True,
            error_type="path_conflict",
            detail="PATH resolves a different OpenAgent executable after update",
        )

    version_result = _run(runner, [str(active), "version"], limit=64 * 1024)
    verified = _version(_one_line(version_result))
    if (
        version_result.returncode != 0
        or verified is None
        or (expected is not None and _version(expected) != verified)
    ):
        return SelfUpdateResult(
            plan=plan,
            ok=False,
            ran=True,
            verified_version=verified,
            error_type="version_verification_failed",
            detail=f"active executable did not report expected version {expected or 'unknown'}",
        )

    doctor = _run(runner, [str(active), "doctor", "--json"])
    payload: Any = None
    try:
        payload = json.loads(doctor.stdout)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    backup = _backup_path(payload)
    if doctor.returncode not in {0, 1} or not isinstance(payload, dict):
        kind = "migration_failed" if doctor.returncode == 3 else "database_unhealthy"
        return SelfUpdateResult(
            plan=plan,
            ok=False,
            ran=True,
            verified_version=verified,
            doctor_exit_code=doctor.returncode,
            backup_path=backup,
            error_type=kind,
            detail=(
                "update installed, but Doctor reports a database migration failure"
                if doctor.returncode == 3
                else "update installed, but Doctor could not verify database health"
            ),
        )

    reported_exit = payload.get("exit_code")
    if not isinstance(reported_exit, int) or reported_exit not in {0, 1}:
        return SelfUpdateResult(
            plan=plan,
            ok=False,
            ran=True,
            verified_version=verified,
            doctor_exit_code=doctor.returncode,
            backup_path=backup,
            error_type="doctor_contract_failed",
            detail="Doctor JSON did not report a valid healthy/warning exit code",
        )

    revised = plan.model_copy(
        update={
            "current_version": verified,
            "latest_version": expected or verified,
            "update_available": False,
            "detail": f"updated to {verified} and verified",
        }
    )
    return SelfUpdateResult(
        plan=revised,
        ok=True,
        ran=True,
        verified_version=verified,
        doctor_exit_code=doctor.returncode,
        backup_path=backup,
        detail=f"updated to {verified}; exact executable and Doctor verified",
    )
