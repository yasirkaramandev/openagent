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
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from .. import __version__
from ..core.versioning import canonical_version, compare_versions, is_newer
from ..credentials.redaction import redact
from ..runtimes.cli.locator import CommandResult, run_bounded
from ..runtimes.cli.updates import (
    CHECK_TIMEOUT_SECONDS,
    MAX_HTTP_BODY_BYTES,
    MAX_UPDATE_OUTPUT_BYTES,
    UPDATE_LOCK_TIMEOUT,
    UPDATE_TIMEOUT_SECONDS,
    JsonFetcher,
    fetch_json,
    update_environment,
)
from ..security.atomic import atomic_write_text
from ..security.file_lock import LockTimeout, file_lock
from .update_safety import (
    AncestryOracle,
    CommitRelation,
    ProcessSafety,
    ProcessSafetyState,
    classify_commit_relation,
    describe_process_block,
    describe_relation_block,
    probe_process_safety,
    relation_permits_install,
    update_permitted,
)

OFFICIAL_REPOSITORY = "yasirkaramandev/openagent"
PYPI_METADATA_URL = "https://pypi.org/pypi/openagent/json"

#: The only repositories a VCS self-update is ever allowed to install from. A value read out of
#: ``install.json`` or a ``direct_url.json`` can never widen this set — an attacker who can write the
#: metadata file still cannot redirect the updater at a repository they control (spec §8, §23).
OFFICIAL_REPOSITORY_ALLOWLIST = frozenset({OFFICIAL_REPOSITORY})
#: The protected branch the ``candidate`` channel tracks. This name is only ever used to *discover*
#: the exact commit to install; it is never itself passed to ``uv`` as an install requirement.
CANDIDATE_BRANCH = "release-candidate"
#: The branch the opt-in ``dev`` channel tracks.
DEV_BRANCH = "main"
GITHUB_API_BASE = "https://api.github.com"
#: The canonical HTTPS clone URL. All VCS installs are pinned to an exact 40-hex commit appended to
#: this base, never to a branch name.
OFFICIAL_HTTPS_REMOTE = f"https://github.com/{OFFICIAL_REPOSITORY}.git"
INSTALL_METADATA_SCHEMA = 1
_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")

SelfUpdateSource = Literal[
    "source-checkout", "uv-tool", "pipx", "pip", "official-github-vcs", "unsupported"
]


class OpenAgentUpdateChannel(str, Enum):
    """The three official update channels (spec §6).

    ``stable`` tracks the latest published non-prerelease release; ``candidate`` tracks the protected
    ``release-candidate`` branch used for RC soak; ``dev`` is an opt-in channel tracking ``main``
    HEAD. The value is the wire form written into ``install.json`` and printed by the CLI.
    """

    STABLE = "stable"
    CANDIDATE = "candidate"
    DEV = "dev"

    @classmethod
    def parse(cls, value: str | None) -> OpenAgentUpdateChannel | None:
        if value is None:
            return None
        try:
            return cls(value.strip().lower())
        except ValueError:
            return None


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
    #: For a source checkout: the version the checkout's source tree declares. This is distinct from
    #: ``current_version`` (the version the *active binary* reports) because a non-editable install
    #: is a copy — the checkout can move ahead of the copy that is actually on PATH (spec §3).
    source_version: str | None = None
    #: The checkout's HEAD is behind official ``origin/main``.
    revision_update_available: bool = False
    #: The active binary's version differs from the source checkout's declared version — the copy on
    #: PATH is stale even though the checkout may be current.
    installation_drift: bool = False
    #: The platform installer must run to reconcile the active binary with the source, whether that
    #: is because of a revision change, version drift, or an explicit repair request (spec §3).
    needs_reinstall: bool = False
    # --- channel-based (checkout-independent) update fields (spec §5-§12) ----------------------
    #: The resolved update channel, when this plan installs an official VCS commit.
    channel: OpenAgentUpdateChannel | None = None
    #: The branch the channel discovers its target commit from (informational; never an install arg).
    channel_ref: str | None = None
    #: The exact 40-hex commit the active binary was built from, when provable from provenance.
    installed_commit: str | None = None
    #: The exact 40-hex commit this plan installs.
    target_commit: str | None = None
    #: The pip requirement (``git+https://…@<sha>``) or index name this plan installs.
    package_url: str | None = None
    #: True when the resolved target orders *below* the installed version; blocked without override.
    is_downgrade: bool = False
    #: Where the target commit sits on the commit graph relative to the last accepted commit. This,
    #: not the version string, is what decides whether an install moves forward (spec §3.2).
    commit_relation: CommitRelation = CommitRelation.SAME
    #: True when this plan migrates a legacy local-checkout install onto an official channel.
    migrating: bool = False
    #: A stable, machine-readable reason for ``update_available``/``can_update`` (spec §19.5).
    reason: str | None = None
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
    #: The exact commit the active binary reports after a successful VCS update.
    verified_commit: str | None = None
    #: True when a failed install was reverted to the previously-installed exact source (spec §17).
    rolled_back: bool = False
    #: Whether ``install.json`` was written. False means the binary is correct but this installation
    #: can no longer prove its channel or its last accepted commit (spec §3.3).
    metadata_persisted: bool = True
    #: Machine-readable outcome. ``installed_with_metadata_warning`` is deliberately distinct from
    #: ``installed``: the binary is good, but provenance was lost and needs ``update --repair``.
    status: str = "installed"
    detail: str = ""


class VcsProvenance(BaseModel):
    """A trusted PEP 610 ``vcs_info`` payload proving an install came from an exact official commit."""

    model_config = ConfigDict(extra="forbid")

    url: str
    repository: str
    requested_revision: str | None
    commit_id: str


class SelfUpdateTarget(BaseModel):
    """A fully-resolved, exact-commit update target safe to show before any mutation (spec §11)."""

    model_config = ConfigDict(extra="forbid")

    channel: OpenAgentUpdateChannel
    repository: str
    ref: str | None
    commit_sha: str
    version: str
    package_url: str
    source_kind: str
    published_at: datetime | None = None
    ci_verified: bool = False
    prerelease: bool = False


class InstallMetadata(BaseModel):
    """Persistent, secret-free record of how OpenAgent was installed (spec §8).

    Unknown fields are preserved on read so a newer binary that writes extra keys does not lose them
    when an older binary rewrites the file; malformed files are quarantined rather than trusted.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: int = INSTALL_METADATA_SCHEMA
    manager: str = "uv-tool"
    source: str = "official-github-vcs"
    repository: str = OFFICIAL_REPOSITORY
    channel: OpenAgentUpdateChannel = OpenAgentUpdateChannel.CANDIDATE
    channel_ref: str | None = CANDIDATE_BRANCH
    installed_version: str | None = None
    installed_commit: str | None = None
    last_accepted_version: str | None = None
    last_accepted_commit: str | None = None
    python: str | None = None
    updated_at: str | None = None


SelfUpdateRunner = Callable[
    [Sequence[str], int, int, Mapping[str, str], Path | None], CommandResult
]
ExecutableResolver = Callable[[str], str | None]
#: Resolves an official update channel to an exact commit target, or ``None`` when it cannot.
TargetResolver = Callable[[OpenAgentUpdateChannel], "SelfUpdateTarget | None"]
#: Reads the exact VCS commit an installed OpenAgent distribution was built from, given its binary.
CommitReader = Callable[[Path], "str | None"]
#: Classifies what is currently using this installation (spec §3.1). May also return a bare list of
#: process identities, which :func:`_coerce_safety` reads as "an idle second process".
ProcessProbe = Callable[[], "ProcessSafety | list[str]"]
#: Persists install metadata (defaults to :func:`write_install_metadata`).
MetadataWriter = Callable[..., None]


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


# The single version authority. The previous local ``_version``/``_version_tuple``/``_compare``
# helpers re-derived version parsing with a regex and an integer tuple, silently discarding
# prerelease metadata — so ``0.1.6rc1`` parsed to ``0.1.6`` and a release candidate compared equal
# to its release. All version questions now go through ``core.versioning`` (spec §4).
_version = canonical_version
_compare = is_newer


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
    repair: bool = False,
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

    # A source checkout that cannot state its own version cannot be reconciled against the active
    # binary. Fail closed rather than guessing the checkout is current (spec §3).
    source_version = _source_version(root)
    if source_version is None or canonical_version(source_version) is None:
        return _blocked_plan(
            current_version=current_version,
            active=active,
            source="source-checkout",
            method="git-origin-main",
            checkout_root=root,
            detail="source checkout does not declare a parseable OpenAgent version",
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

    # Two independent axes decide whether an update is needed (spec §3). A checkout that is level
    # with origin/main can still be serving an old binary, because a non-editable install is a copy
    # of the source, not the source itself.
    revision_update_available = local != remote
    active_canonical = canonical_version(current_version)
    source_canonical = canonical_version(source_version)
    if active_canonical is None:
        # We cannot prove the active binary is current, so we must not assume it is (Case E). Treat
        # an unreadable/unparseable active version as drift and reinstall.
        installation_drift = True
    else:
        installation_drift = active_canonical != source_canonical
    needs_reinstall = revision_update_available or installation_drift or repair
    update_available = needs_reinstall

    commands: list[list[str]] = []
    if revision_update_available:
        # Only fast-forward when the checkout is actually behind. When it is level with origin/main
        # but the binary is stale, the pull would be a no-op; the reinstall is what repairs it.
        commands.append(["git", "-C", str(root), "pull", "--ff-only", "origin", "main"])
    commands.append(command)

    if revision_update_available:
        detail = f"official main has a newer revision ({local[:12]} -> {remote[:12]})"
    elif repair and not installation_drift:
        detail = f"reinstalling {source_canonical} from the current checkout on request"
    elif installation_drift:
        detail = (
            f"installed binary reports {current_version} but the checkout is {source_version}; "
            "reinstalling from source"
        )
    else:
        detail = f"current at official main revision {local[:12]} ({source_canonical})"

    return SelfUpdatePlan(
        current_version=current_version,
        latest_version=source_version,
        source="source-checkout",
        active_executable=str(active),
        resolved_executable=str(_resolved(active)),
        check_method="git-origin-main",
        update_available=update_available,
        can_update=True,
        commands=commands,
        checkout_root=str(root),
        local_revision=local,
        remote_revision=remote,
        source_version=source_version,
        revision_update_available=revision_update_available,
        installation_drift=installation_drift,
        needs_reinstall=needs_reinstall,
        detail=detail,
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
    metadata: InstallMetadata | None | Literal[False] = False,
    channel: OpenAgentUpdateChannel | str | None = None,
    runner: SelfUpdateRunner = run_self_update_command,
    fetcher: JsonFetcher = fetch_json,
    target_resolver: TargetResolver | None = None,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    repair: bool = False,
    allow_downgrade: bool = False,
    local_dev: bool = False,
    compare: AncestryOracle | None = None,
) -> SelfUpdatePlan:
    """Resolve provenance and check the matching official update source, checkout-independently.

    ``direct_url=False``/``metadata=False`` mean "read installed metadata from disk"; tests pass an
    explicit value (including ``None``) to model a specific install without touching the real home.

    A VCS install, an install recorded in ``install.json`` as ``official-github-vcs``, or a legacy
    ``file://`` local checkout (unless ``local_dev`` is set) all resolve to a checkout-independent
    channel plan — the user never needs a clean ``main`` checkout to update (spec §5, §10).
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

    environment = dict(os.environ if environ is None else environ)
    payload = _direct_url_payload() if direct_url is False else direct_url
    provenance = _vcs_provenance(payload)
    checkout = _file_checkout(payload)
    install_meta = read_install_metadata(environ=environment) if metadata is False else metadata
    explicit_channel = (
        channel
        if isinstance(channel, OpenAgentUpdateChannel)
        else OpenAgentUpdateChannel.parse(channel)
    )
    metadata_is_vcs = install_meta is not None and install_meta.source == "official-github-vcs"

    # Case A — steady state: a VCS install, or one install.json records as an official channel
    # install. This is where every install lands after the first migration.
    if provenance is not None or metadata_is_vcs:
        installed_commit = (
            provenance.commit_id
            if provenance is not None
            else (install_meta.installed_commit if install_meta else None)
        )
        return _channel_plan(
            current_version=current_version,
            active=active,
            metadata=install_meta,
            explicit_channel=explicit_channel,
            installed_commit=installed_commit,
            migrating=False,
            repair=repair,
            allow_downgrade=allow_downgrade,
            runner=runner,
            fetcher=fetcher,
            target_resolver=target_resolver,
            environ=environment,
            compare=compare,
        )

    # Case B — legacy local-directory install. By default migrate it onto the official channel
    # without ever consulting the checkout's branch or cleanliness. ``local_dev`` keeps the old
    # source-checkout developer workflow for someone who explicitly opts in (spec §10, §20.2).
    if checkout is not None:
        if local_dev:
            return _source_plan(
                root=checkout,
                current_version=current_version,
                active=active,
                runner=runner,
                platform=sys.platform if platform is None else platform,
                repair=repair,
            )
        return _channel_plan(
            current_version=current_version,
            active=active,
            metadata=install_meta,
            explicit_channel=explicit_channel,
            installed_commit=None,
            migrating=True,
            repair=repair,
            allow_downgrade=allow_downgrade,
            runner=runner,
            fetcher=fetcher,
            target_resolver=target_resolver,
            environ=environment,
            compare=compare,
        )

    # Case C — a remote/malformed direct-URL that is not a trusted official VCS install.
    if payload is not None:
        return _blocked_plan(
            current_version=current_version,
            active=active,
            source="unsupported",
            method="direct-url",
            detail="remote or malformed direct-URL installs cannot be updated safely in place",
        )

    # Case D — an index install (no direct_url). An explicit prerelease channel resolves to a VCS
    # install (candidate/dev are not on the index); otherwise use the proven PyPI upgrade path.
    if explicit_channel in {OpenAgentUpdateChannel.CANDIDATE, OpenAgentUpdateChannel.DEV}:
        return _channel_plan(
            current_version=current_version,
            active=active,
            metadata=install_meta,
            explicit_channel=explicit_channel,
            installed_commit=install_meta.installed_commit if install_meta else None,
            migrating=True,
            repair=repair,
            allow_downgrade=allow_downgrade,
            runner=runner,
            fetcher=fetcher,
            target_resolver=target_resolver,
            environ=environment,
            compare=compare,
        )
    if repair:
        return _blocked_plan(
            current_version=current_version,
            active=active,
            source="unsupported",
            method="repair",
            detail=(
                "official index distribution could not be verified; use the proven "
                "source-checkout repair path"
            ),
        )

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


def _channel_plan(
    *,
    current_version: str,
    active: Path,
    metadata: InstallMetadata | None,
    explicit_channel: OpenAgentUpdateChannel | None,
    installed_commit: str | None,
    migrating: bool,
    repair: bool,
    allow_downgrade: bool,
    runner: SelfUpdateRunner,
    fetcher: JsonFetcher,
    target_resolver: TargetResolver | None,
    environ: Mapping[str, str],
    compare: AncestryOracle | None = None,
) -> SelfUpdatePlan:
    """Resolve the effective channel and its exact-commit target, then build the plan."""

    channel = _infer_channel(
        explicit=explicit_channel, metadata=metadata, current_version=current_version
    )
    resolver = target_resolver or (
        lambda ch: resolve_vcs_target(ch, runner=runner, fetcher=fetcher)
    )
    target = resolver(channel)
    uv_path = shutil.which("uv", path=environ.get("PATH"))
    python_version = (metadata.python if metadata and metadata.python else None) or "3.12"
    return _vcs_plan(
        channel=channel,
        target=target,
        current_version=current_version,
        installed_commit=installed_commit,
        metadata=metadata,
        active=active,
        uv_path=uv_path,
        python_version=python_version,
        allow_downgrade=allow_downgrade,
        migrating=migrating,
        repair=repair,
        compare=compare or (lambda base, head: _github_commit_status(fetcher, base, head)),
    )


def _github_commit_status(fetcher: JsonFetcher, base: str, head: str) -> str | None:
    """Ask GitHub how ``head`` relates to ``base``: identical, ahead, behind, or diverged.

    Credential-free and read-only against the official public repository. ``None`` on any failure —
    a rate limit or a network error must not be able to *manufacture* a forward relationship, and
    the caller treats an unanswered comparison as UNKNOWN rather than as permission.
    """

    url = f"{GITHUB_API_BASE}/repos/{OFFICIAL_REPOSITORY}/compare/{base}...{head}"
    try:
        payload = fetcher(url, CHECK_TIMEOUT_SECONDS, MAX_HTTP_BODY_BYTES)
    except Exception:
        return None
    status = payload.get("status") if isinstance(payload, dict) else None
    return status if isinstance(status, str) else None


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
    commit_reader: CommitReader | None = None,
    process_probe: ProcessProbe | None = None,
    metadata_writer: MetadataWriter | None = None,
    lock_path: str | Path | None = None,
    lock_timeout: float = UPDATE_LOCK_TIMEOUT,
    environ: Mapping[str, str] | None = None,
    force: bool = False,
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

    if plan.source == "official-github-vcs":
        return _perform_vcs_update(
            plan,
            runner=runner,
            resolver=resolver,
            commit_reader=commit_reader or _read_installed_vcs_commit,
            process_probe=process_probe or probe_process_safety,
            metadata_writer=metadata_writer or write_install_metadata,
            lock_path=lock_path,
            lock_timeout=lock_timeout,
            environ=environ,
            force=force,
        )

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


# ======================================================================================
# Checkout-independent, channel-based updates (spec §5-§19)
# ======================================================================================


def openagent_home(environ: Mapping[str, str] | None = None) -> Path:
    """The ``~/.openagent`` data directory, honouring ``OPENAGENT_HOME`` (spec §8)."""

    source = os.environ if environ is None else environ
    override = source.get("OPENAGENT_HOME")
    if override:
        return Path(override).expanduser()
    return Path(source.get("HOME", str(Path.home()))).expanduser() / ".openagent"


def install_metadata_path(environ: Mapping[str, str] | None = None) -> Path:
    return openagent_home(environ) / "install.json"


def self_update_lock_path(environ: Mapping[str, str] | None = None) -> Path:
    return openagent_home(environ) / "locks" / "self-update.lock"


def _normalize_repository(url: str) -> str | None:
    """Reduce a git URL to ``owner/name`` iff it is an official, credential-free HTTPS/SSH remote.

    Anything with embedded userinfo (``https://token@github.com/...``), an unencrypted ``http`` URL,
    or a host that is not github.com returns ``None`` and is rejected by the caller (spec §9).
    """

    text = url.strip()
    if "@" in text and text.lower().startswith(("http://", "https://")):
        # Reject a URL that smuggles credentials in the authority: https://user:tok@github.com/...
        authority = text.split("//", 1)[1].split("/", 1)[0]
        if "@" in authority:
            return None
    lowered = text.lower()
    for prefix in ("https://github.com/", "ssh://git@github.com/"):
        if lowered.startswith(prefix):
            path = text[len(prefix) :]
            break
    else:
        if lowered.startswith("git@github.com:"):
            path = text[len("git@github.com:") :]
        else:
            return None
    path = path.strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return "/".join(parts)


def _vcs_provenance(payload: Mapping[str, Any] | None) -> VcsProvenance | None:
    """Parse a trusted PEP 610 VCS ``direct_url.json`` payload, or ``None`` when it is not one.

    A payload is trusted only when it is a git VCS install from an official, credential-free remote
    with an exact 40-hex ``commit_id``. Malformed VCS metadata fails closed (returns ``None``); the
    caller then refuses to treat the install as updatable rather than guessing (spec §9).
    """

    if not payload or not isinstance(payload.get("vcs_info"), Mapping):
        return None
    info = payload["vcs_info"]
    url = payload.get("url")
    if not isinstance(url, str) or info.get("vcs") != "git":
        return None
    commit = info.get("commit_id")
    if not isinstance(commit, str) or not _SHA_RE.match(commit.strip().lower()):
        return None
    repository = _normalize_repository(url)
    if repository is None or repository not in OFFICIAL_REPOSITORY_ALLOWLIST:
        return None
    requested = info.get("requested_revision")
    return VcsProvenance(
        url=OFFICIAL_HTTPS_REMOTE,
        repository=repository,
        requested_revision=requested if isinstance(requested, str) else None,
        commit_id=commit.strip().lower(),
    )


def read_install_metadata(
    path: Path | None = None, *, environ: Mapping[str, str] | None = None
) -> InstallMetadata | None:
    """Read ``install.json``. A malformed file is quarantined (``*.corrupt``) and treated as absent."""

    target = path or install_metadata_path(environ)
    try:
        raw = target.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        _quarantine(target)
        return None
    if not isinstance(value, dict):
        _quarantine(target)
        return None
    try:
        return InstallMetadata.model_validate(value)
    except Exception:
        _quarantine(target)
        return None


def _quarantine(path: Path) -> None:
    try:
        path.replace(path.with_suffix(path.suffix + ".corrupt"))
    except OSError:
        pass


def write_install_metadata(
    metadata: InstallMetadata,
    path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Atomically persist ``install.json`` at ``0600`` under the cross-process self-update lock.

    Unknown fields already on disk are preserved (an older writer must not drop a newer schema's
    keys); the repository field can never be widened beyond the compiled allowlist (spec §8).
    """

    target = path or install_metadata_path(environ)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = read_install_metadata(target)
    merged: dict[str, Any] = {}
    if existing is not None:
        merged.update(existing.model_dump(mode="json"))
    merged.update(metadata.model_dump(mode="json"))
    if merged.get("repository") not in OFFICIAL_REPOSITORY_ALLOWLIST:
        merged["repository"] = OFFICIAL_REPOSITORY
    merged["schema_version"] = INSTALL_METADATA_SCHEMA
    merged["updated_at"] = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(merged, indent=2, sort_keys=True)
    if len(payload.encode("utf-8")) > 64 * 1024:
        raise ValueError("install metadata exceeds the 64 KiB bound")
    atomic_write_text(target, payload, mode=0o600)


def _infer_channel(
    *,
    explicit: OpenAgentUpdateChannel | None,
    metadata: InstallMetadata | None,
    current_version: str,
) -> OpenAgentUpdateChannel:
    """Resolve the effective channel: explicit CLI option → install.json → version heuristic.

    ``dev`` is never inferred — it is opt-in only. An installed prerelease implies the ``candidate``
    channel; a final release implies ``stable`` (spec §6.4).
    """

    if explicit is not None:
        return explicit
    if metadata is not None and metadata.channel is not None:
        return metadata.channel
    parsed = _version(current_version)
    if parsed is None:
        # Fail closed toward candidate: an unparseable running version is far more likely a
        # prerelease/dev build than a shipped final, and candidate is the safer soak channel.
        return OpenAgentUpdateChannel.CANDIDATE
    from packaging.version import Version

    return (
        OpenAgentUpdateChannel.CANDIDATE
        if Version(parsed).is_prerelease
        else OpenAgentUpdateChannel.STABLE
    )


def _is_prerelease(version: str | None) -> bool:
    parsed = _version(version)
    if parsed is None:
        return False
    from packaging.version import Version

    return Version(parsed).is_prerelease


# The credential-free, non-interactive environment for every git subprocess the updater runs. A
# public HTTPS clone needs no authentication; these settings guarantee git never blocks on a
# credential prompt, never invokes a credential helper, and never shells out to askpass (spec §16).
_GIT_NO_CREDENTIALS_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GCM_INTERACTIVE": "never",
}


def _git_ls_remote_sha(
    runner: SelfUpdateRunner,
    ref: str,
    *,
    tags: bool = False,
    url: str = OFFICIAL_HTTPS_REMOTE,
) -> str | None:
    """Resolve an exact 40-hex commit for a branch or tag with a fixed-argv, shell-free git call.

    The remote branch name is only ever an argument to ``git ls-remote`` here — it is never
    interpolated into a shell string and never becomes a pip install requirement (spec §11.1).
    """

    flag = "--tags" if tags else "--heads"
    argv = ["git", "-c", "credential.helper=", "ls-remote", flag, url, ref]
    result = _run(
        runner,
        argv,
        timeout=CHECK_TIMEOUT_SECONDS,
        extra_env=_GIT_NO_CREDENTIALS_ENV,
    )
    if result.returncode != 0:
        return None
    dereferenced: str | None = None
    plain: str | None = None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, name = parts[0].strip().lower(), parts[1]
        if not _SHA_RE.match(sha):
            continue
        # An annotated tag lists both the tag object and, via ``^{}``, the commit it points at. The
        # commit is what we install, so it wins over the plain ref when both appear.
        if name.endswith("^{}"):
            dereferenced = sha
        else:
            plain = sha
    return dereferenced or plain


def _remote_version_at(fetcher: JsonFetcher, commit_sha: str) -> str | None:
    """Best-effort read of ``__version__`` at an exact commit via the GitHub contents API.

    A failure here (rate limit, network, layout change) yields ``None``: the resolved target still
    installs by exact commit, and ``perform_self_update`` verifies the *actual* installed version
    after the fact. So an unknown version at check time never blocks an update (spec §11.1).
    """

    import base64

    url = (
        f"{GITHUB_API_BASE}/repos/{OFFICIAL_REPOSITORY}"
        f"/contents/src/openagent/__init__.py?ref={commit_sha}"
    )
    try:
        payload = fetcher(url, CHECK_TIMEOUT_SECONDS, MAX_HTTP_BODY_BYTES)
    except Exception:
        return None
    content = payload.get("content")
    if not isinstance(content, str) or payload.get("encoding") != "base64":
        return None
    try:
        text = base64.b64decode(content).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return None
    match = re.search(r'(?m)^__version__\s*=\s*["\']([^"\']+)["\']\s*$', text)
    return match.group(1) if match else None


def _latest_stable_target(
    fetcher: JsonFetcher, runner: SelfUpdateRunner
) -> SelfUpdateTarget | None:
    """Resolve the latest published, non-prerelease GitHub Release to an exact commit (spec §11.2).

    ``/releases/latest`` is defined by GitHub to exclude drafts and prereleases, so a draft PR head
    or an ``rc`` tag can never be a stable target. The tag is then dereferenced to its exact commit.
    """

    try:
        payload = fetcher(
            f"{GITHUB_API_BASE}/repos/{OFFICIAL_REPOSITORY}/releases/latest",
            CHECK_TIMEOUT_SECONDS,
            MAX_HTTP_BODY_BYTES,
        )
    except Exception:
        return None
    if payload.get("draft") or payload.get("prerelease"):
        return None
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag:
        return None
    version = tag.lstrip("vV")
    if _version(version) is None or _is_prerelease(version):
        return None
    sha = _git_ls_remote_sha(runner, tag, tags=True)
    if sha is None:
        return None
    published = payload.get("published_at")
    try:
        published_at = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        published_at = None
    return SelfUpdateTarget(
        channel=OpenAgentUpdateChannel.STABLE,
        repository=OFFICIAL_REPOSITORY,
        ref=tag,
        commit_sha=sha,
        version=version,
        package_url=f"git+{OFFICIAL_HTTPS_REMOTE}@{sha}",
        source_kind="github-release",
        published_at=published_at,
        ci_verified=True,
        prerelease=False,
    )


def resolve_vcs_target(
    channel: OpenAgentUpdateChannel,
    *,
    runner: SelfUpdateRunner = run_self_update_command,
    fetcher: JsonFetcher = fetch_json,
) -> SelfUpdateTarget | None:
    """Resolve a channel to an exact-commit target, or ``None`` when nothing is installable.

    ``candidate``/``dev`` discover their commit from a protected branch head; ``stable`` from the
    latest published release. Every target is pinned to a 40-hex commit, never a branch name.
    """

    if channel is OpenAgentUpdateChannel.STABLE:
        return _latest_stable_target(fetcher, runner)
    branch = CANDIDATE_BRANCH if channel is OpenAgentUpdateChannel.CANDIDATE else DEV_BRANCH
    sha = _git_ls_remote_sha(runner, branch)
    if sha is None:
        return None
    version = _remote_version_at(fetcher, sha)
    return SelfUpdateTarget(
        channel=channel,
        repository=OFFICIAL_REPOSITORY,
        ref=branch,
        commit_sha=sha,
        version=version or "",
        package_url=f"git+{OFFICIAL_HTTPS_REMOTE}@{sha}",
        source_kind="github-vcs",
        ci_verified=channel is OpenAgentUpdateChannel.CANDIDATE,
        prerelease=_is_prerelease(version) if version else True,
    )


def _vcs_install_command(uv_path: str, package_url: str, python_version: str) -> list[str]:
    """The canonical exact-commit install argv (spec §16). Always a 40-hex commit, never a branch."""

    return [
        uv_path,
        "tool",
        "install",
        "--force",
        "--reinstall",
        "--refresh",
        "--python",
        python_version,
        package_url,
    ]


def _vcs_plan(
    *,
    channel: OpenAgentUpdateChannel,
    target: SelfUpdateTarget | None,
    current_version: str,
    installed_commit: str | None,
    metadata: InstallMetadata | None,
    active: Path,
    uv_path: str | None,
    python_version: str,
    allow_downgrade: bool,
    migrating: bool,
    repair: bool,
    compare: AncestryOracle | None = None,
) -> SelfUpdatePlan:
    """Build a checkout-independent update plan from a resolved channel target (spec §11-§12)."""

    base: dict[str, Any] = {
        "current_version": current_version,
        "source": "official-github-vcs",
        "active_executable": str(active),
        "resolved_executable": str(_resolved(active)),
        "check_method": "official-github-vcs",
        "channel": channel,
        "installed_commit": installed_commit,
        "migrating": migrating,
    }
    if target is None:
        return SelfUpdatePlan(
            **base,
            can_update=False,
            update_available=None if channel is OpenAgentUpdateChannel.STABLE else False,
            reason="no_target",
            detail=(f"no installable commit is currently published on the {channel.value} channel"),
        )
    if uv_path is None:
        return SelfUpdatePlan(
            **base,
            channel_ref=target.ref,
            target_commit=target.commit_sha,
            can_update=False,
            reason="uv_missing",
            detail="channel updates require uv, but uv is not available on PATH",
        )

    target_version = target.version or None
    # The downgrade baseline is the newer of the running version and the last version we accepted,
    # so a rollback attack cannot walk the install backwards one accepted version at a time (§12).
    baseline = current_version
    if metadata is not None and metadata.last_accepted_version:
        if (compare_versions(metadata.last_accepted_version, baseline) or 0) > 0:
            baseline = metadata.last_accepted_version

    is_downgrade = False
    if target_version is not None:
        ordering = compare_versions(target_version, baseline)
        if ordering is not None and ordering < 0:
            is_downgrade = True

    same_commit = installed_commit is not None and installed_commit == target.commit_sha
    same_version = target_version is not None and _version(target_version) == _version(
        current_version
    )

    # Commit ancestry, not version strings, decides whether this is forward motion (spec §3.2).
    # The baseline is the newest commit this installation has *accepted*, so a rollback cannot walk
    # it backwards one accepted commit at a time. During a release candidate every commit reports
    # the same version, which is exactly when string comparison silently permits going backwards.
    commit_baseline = installed_commit
    if metadata is not None and metadata.last_accepted_commit:
        commit_baseline = metadata.last_accepted_commit
    relation = CommitRelation.SAME
    if not migrating and commit_baseline and compare is not None:
        relation = classify_commit_relation(
            installed=commit_baseline, target=target.commit_sha, compare=compare
        )

    command = _vcs_install_command(uv_path, target.package_url, python_version)
    common: dict[str, Any] = {
        **base,
        "latest_version": target_version,
        "channel_ref": target.ref,
        "target_commit": target.commit_sha,
        "package_url": target.package_url,
        "commands": [command],
        "is_downgrade": is_downgrade or relation is CommitRelation.BEHIND,
        "commit_relation": relation,
    }

    # A version that plainly orders downward is reported as a downgrade, because that is the more
    # useful message. Ancestry is checked next, and catches the case versions cannot see: identical
    # version strings on different commits.
    if is_downgrade and not allow_downgrade:
        return SelfUpdatePlan(
            **common,
            can_update=False,
            update_available=None,
            reason="downgrade_blocked",
            detail=(
                f"target {target_version} on the {channel.value} channel is older than the "
                f"installed {baseline}; re-run with --allow-downgrade to force it"
            ),
        )

    if not relation_permits_install(relation, allow_downgrade=allow_downgrade):
        return SelfUpdatePlan(
            **common,
            can_update=False,
            update_available=None,
            reason=(
                "commit_unknown"
                if relation is CommitRelation.UNKNOWN
                else f"commit_{relation.value}_blocked"
            ),
            detail=describe_relation_block(
                relation, installed=commit_baseline, target=target.commit_sha
            ),
        )

    if same_commit and not repair and not is_downgrade:
        return SelfUpdatePlan(
            **common,
            can_update=True,
            update_available=False,
            reason="up_to_date",
            detail=(
                f"already at the {channel.value} commit {target.commit_sha[:12]}"
                + (f" ({target_version})" if target_version else "")
            ),
        )

    if migrating:
        reason, detail = (
            "migrate",
            f"migrating to the official {channel.value} channel at commit "
            f"{target.commit_sha[:12]}" + (f" ({target_version})" if target_version else ""),
        )
    elif is_downgrade:
        reason, detail = (
            "downgrade",
            f"forcing downgrade to {target_version} at commit {target.commit_sha[:12]}",
        )
    elif same_version and not same_commit:
        # Same version, different commit — only reachable now when ancestry proved the target is a
        # descendant (or the operator passed --allow-downgrade), so "newer" is earned, not assumed.
        reason, detail = (
            "newer_commit",
            f"advancing {target_version or channel.value} to the {channel.value} commit "
            f"{target.commit_sha[:12]} ({relation.value} of the installed commit)",
        )
    elif repair and same_commit:
        reason, detail = (
            "repair",
            f"reinstalling {channel.value} commit {target.commit_sha[:12]} on request",
        )
    else:
        reason, detail = (
            "newer_version",
            f"{current_version} -> {target_version or target.commit_sha[:12]} ({channel.value})",
        )

    return SelfUpdatePlan(
        **common,
        can_update=True,
        update_available=True,
        reason=reason,
        detail=detail,
    )


# ---------------------------------------------------------------- staged install + rollback (spec §14-§18)


def _read_installed_vcs_commit(binary: Path) -> str | None:
    """Read the exact commit an installed OpenAgent was built from, from its ``direct_url.json``.

    The running process still holds the *old* import, so provenance is read off disk from the
    freshly-installed ``*.dist-info`` next to the binary, never via ``importlib.metadata`` (spec §18).
    """

    try:
        resolved = binary.resolve()
    except OSError:
        resolved = binary
    bases: list[Path] = []
    for parent in (resolved.parent, resolved.parent.parent, resolved.parent.parent.parent):
        if parent not in bases:
            bases.append(parent)
    for base in bases:
        try:
            candidates = sorted(base.glob("**/openagent-*.dist-info/direct_url.json"))
        except OSError:
            continue
        for path in candidates:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
            info = data.get("vcs_info") if isinstance(data, dict) else None
            commit = info.get("commit_id") if isinstance(info, dict) else None
            if isinstance(commit, str) and _SHA_RE.match(commit.strip().lower()):
                return commit.strip().lower()
    return None


def _coerce_safety(value: ProcessSafety | Sequence[str]) -> ProcessSafety:
    """Accept either a classified :class:`ProcessSafety` or a bare list of process identities.

    The list form is what the probe returned before spec §3.1: it can say *that* another OpenAgent
    is live but not *what it is doing*, so a non-empty list is treated as an idle second process —
    the weakest positive finding, and the only one ``--force`` may override.
    """

    if isinstance(value, ProcessSafety):
        return value
    identities = [str(item) for item in value]
    if not identities:
        return ProcessSafety(state=ProcessSafetyState.SAFE, detail="no other OpenAgent process")
    return ProcessSafety(
        state=ProcessSafetyState.ACTIVE_IDLE_PROCESS,
        identities=identities[:5],
        detail="another OpenAgent process is live",
    )


def _staged_binary_name() -> str:
    return "openagent.exe" if sys.platform.startswith("win") else "openagent"


def _vcs_result(
    plan: SelfUpdatePlan,
    *,
    ok: bool,
    ran: bool,
    error_type: str | None = None,
    detail: str,
    verified_version: str | None = None,
    verified_commit: str | None = None,
    doctor_exit_code: int | None = None,
    backup_path: str | None = None,
    rolled_back: bool = False,
    metadata_persisted: bool = True,
    status: str = "installed",
) -> SelfUpdateResult:
    return SelfUpdateResult(
        plan=plan,
        ok=ok,
        ran=ran,
        error_type=error_type,
        detail=detail,
        verified_version=verified_version,
        verified_commit=verified_commit,
        doctor_exit_code=doctor_exit_code,
        backup_path=backup_path,
        rolled_back=rolled_back,
        metadata_persisted=metadata_persisted,
        status=status,
    )


def _vcs_rollback(
    plan: SelfUpdatePlan,
    *,
    error_type: str,
    detail: str,
    install_argv: Sequence[str],
    runner: SelfUpdateRunner,
    resolver: ExecutableResolver,
) -> SelfUpdateResult:
    """Reinstall the previously-installed exact commit after a failed install (spec §17.1).

    Never called once the new binary has migrated the database (spec §17.2): an older binary may not
    be able to read a newer schema, so a post-migration failure keeps the new binary and surfaces the
    backup path instead.
    """

    previous = plan.installed_commit
    active = Path(plan.active_executable)
    if previous is None:
        return _vcs_result(
            plan,
            ok=False,
            ran=True,
            error_type=error_type,
            detail=(
                f"{detail}; no previous exact commit was recorded, so no automatic rollback was "
                f"possible — reinstall a known-good commit with: "
                f"uv tool install --force --reinstall {plan.package_url}"
            ),
        )
    rollback_url = f"git+{OFFICIAL_HTTPS_REMOTE}@{previous}"
    rollback_argv = list(install_argv[:-1]) + [rollback_url]
    rb = _run(runner, rollback_argv, timeout=UPDATE_TIMEOUT_SECONDS)
    if rb.returncode != 0:
        return _vcs_result(
            plan,
            ok=False,
            ran=True,
            error_type="critical_update_recovery_failed",
            detail=(
                f"{detail}; rollback to previous commit {previous[:12]} also failed "
                f"({_command_text(rb)}). Repair manually with: {' '.join(rollback_argv)}"
            ),
        )
    version_result = _run(runner, [str(active), "version"], limit=64 * 1024)
    return _vcs_result(
        plan,
        ok=False,
        ran=True,
        rolled_back=True,
        error_type=error_type,
        verified_version=_version(_one_line(version_result)),
        verified_commit=previous,
        detail=f"{detail}; rolled back to previous commit {previous[:12]}",
    )


def _perform_vcs_update(
    plan: SelfUpdatePlan,
    *,
    runner: SelfUpdateRunner,
    resolver: ExecutableResolver,
    commit_reader: CommitReader,
    process_probe: ProcessProbe,
    metadata_writer: MetadataWriter,
    lock_path: str | Path | None,
    lock_timeout: float,
    environ: Mapping[str, str] | None,
    force: bool,
) -> SelfUpdateResult:
    environment = dict(os.environ if environ is None else environ)
    lock_file = Path(lock_path) if lock_path is not None else self_update_lock_path(environment)
    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _vcs_result(
            plan,
            ok=False,
            ran=False,
            error_type="lock_unavailable",
            detail=f"the self-update lock directory could not be created ({exc})",
        )
    try:
        with file_lock(lock_file, timeout=lock_timeout):
            return _perform_vcs_update_locked(
                plan,
                runner=runner,
                resolver=resolver,
                commit_reader=commit_reader,
                process_probe=process_probe,
                metadata_writer=metadata_writer,
                environment=environment,
                force=force,
            )
    except LockTimeout:
        return _vcs_result(
            plan,
            ok=False,
            ran=False,
            error_type="update_in_progress",
            detail="another OpenAgent update is already running",
        )
    except OSError as exc:
        return _vcs_result(
            plan,
            ok=False,
            ran=False,
            error_type="lock_unavailable",
            detail=f"the self-update lock could not be taken ({exc}); refusing to update",
        )


def _perform_vcs_update_locked(
    plan: SelfUpdatePlan,
    *,
    runner: SelfUpdateRunner,
    resolver: ExecutableResolver,
    commit_reader: CommitReader,
    process_probe: ProcessProbe,
    metadata_writer: MetadataWriter,
    environment: Mapping[str, str],
    force: bool,
) -> SelfUpdateResult:
    if not plan.commands or not plan.package_url or not plan.target_commit:
        return _vcs_result(
            plan,
            ok=False,
            ran=False,
            error_type="update_blocked",
            detail="channel plan is missing an install command or target commit",
        )
    install_argv = list(plan.commands[0])
    target_commit = plan.target_commit
    target_version = plan.latest_version
    active = Path(plan.active_executable)

    # Active-process safety (spec §3.1). --force is an override for an idle second process only.
    # An active run, an open TUI, or a probe that could not run all block regardless of --force:
    # the first two are live writers, and the third is an unanswered question, not a clear result.
    safety = _coerce_safety(process_probe())
    if not update_permitted(safety, force=force):
        return _vcs_result(
            plan,
            ok=False,
            ran=False,
            error_type=(
                "process_unknown"
                if safety.state is ProcessSafetyState.UNKNOWN
                else "process_active"
            ),
            detail=describe_process_block(safety, force=force),
        )

    # 1) Stage the exact commit into an isolated tool area and prove it before touching the active
    #    installation (spec §15). A staging failure must leave the active install untouched.
    with tempfile.TemporaryDirectory(prefix="openagent-stage-") as tmp:
        staging_root = Path(tmp)
        tool_dir = staging_root / "tools"
        bin_dir = staging_root / "bin"
        stage_env = {"UV_TOOL_DIR": str(tool_dir), "UV_TOOL_BIN_DIR": str(bin_dir)}
        staged = _run(runner, install_argv, timeout=UPDATE_TIMEOUT_SECONDS, extra_env=stage_env)
        if staged.returncode != 0:
            return _vcs_result(
                plan,
                ok=False,
                ran=False,
                error_type="staging_failed",
                detail=f"staged install failed, active install untouched: {_command_text(staged)}",
            )
        staged_bin = bin_dir / _staged_binary_name()
        staged_version_result = _run(runner, [str(staged_bin), "version"], limit=64 * 1024)
        staged_version = _version(_one_line(staged_version_result))
        if staged_version_result.returncode != 0 or staged_version is None:
            return _vcs_result(
                plan,
                ok=False,
                ran=False,
                error_type="staging_failed",
                detail="staged install did not report a version; active install untouched",
            )
        if (
            target_version
            and _version(target_version)
            and _version(target_version) != staged_version
        ):
            return _vcs_result(
                plan,
                ok=False,
                ran=False,
                error_type="staging_failed",
                detail=(
                    f"staged install is {staged_version}, expected {target_version}; "
                    "active install untouched"
                ),
            )
        staged_commit = commit_reader(staged_bin)
        if staged_commit != target_commit:
            return _vcs_result(
                plan,
                ok=False,
                ran=False,
                error_type="staging_failed",
                detail=(
                    f"staged install commit {staged_commit or 'unknown'} != target "
                    f"{target_commit[:12]}; active install untouched"
                ),
            )

    # 2) Promote: install the exact same commit into the active tool environment.
    promoted = _run(runner, install_argv, timeout=UPDATE_TIMEOUT_SECONDS)
    if promoted.returncode != 0:
        return _vcs_rollback(
            plan,
            error_type="update_command_failed",
            detail=f"active install failed: {_command_text(promoted)}",
            install_argv=install_argv,
            runner=runner,
            resolver=resolver,
        )

    # 3) Verify the exact active binary, its PATH resolution, version, and commit (spec §18).
    resolved_by_name = resolver("openagent")
    if resolved_by_name is None or _resolved(Path(resolved_by_name)) != _resolved(active):
        return _vcs_rollback(
            plan,
            error_type="path_conflict",
            detail="PATH resolves a different OpenAgent executable after update",
            install_argv=install_argv,
            runner=runner,
            resolver=resolver,
        )
    version_result = _run(runner, [str(active), "version"], limit=64 * 1024)
    verified = _version(_one_line(version_result))
    if (
        version_result.returncode != 0
        or verified is None
        or (target_version and _version(target_version) and _version(target_version) != verified)
    ):
        return _vcs_rollback(
            plan,
            error_type="version_verification_failed",
            detail=f"active executable did not report expected version {target_version or 'unknown'}",
            install_argv=install_argv,
            runner=runner,
            resolver=resolver,
        )
    installed_commit = commit_reader(active)
    if installed_commit != target_commit:
        return _vcs_rollback(
            plan,
            error_type="commit_verification_failed",
            detail=(
                f"active executable commit {installed_commit or 'unknown'} != target "
                f"{target_commit[:12]} after update"
            ),
            install_argv=install_argv,
            runner=runner,
            resolver=resolver,
        )

    # 4) Doctor. From here the new binary may have migrated the database, so a failure never rolls
    #    back to the old binary (spec §17.2) — it surfaces the backup path and stays on the new one.
    doctor = _run(runner, [str(active), "doctor", "--json"])
    payload: Any = None
    try:
        payload = json.loads(doctor.stdout)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    backup = _backup_path(payload)
    if doctor.returncode == 3:
        return _vcs_result(
            plan,
            ok=False,
            ran=True,
            error_type="migration_failed",
            verified_version=verified,
            verified_commit=installed_commit,
            doctor_exit_code=3,
            backup_path=backup,
            detail="update installed, but Doctor reports a database migration failure",
        )
    if doctor.returncode not in {0, 1} or not isinstance(payload, dict):
        return _vcs_result(
            plan,
            ok=False,
            ran=True,
            error_type="database_unhealthy",
            verified_version=verified,
            verified_commit=installed_commit,
            doctor_exit_code=doctor.returncode,
            backup_path=backup,
            detail="update installed, but Doctor could not verify database health",
        )
    reported_exit = payload.get("exit_code")
    if not isinstance(reported_exit, int) or reported_exit not in {0, 1}:
        return _vcs_result(
            plan,
            ok=False,
            ran=True,
            error_type="doctor_contract_failed",
            verified_version=verified,
            verified_commit=installed_commit,
            doctor_exit_code=doctor.returncode,
            backup_path=backup,
            detail="Doctor JSON did not report a valid healthy/warning exit code",
        )

    # 5) Success — persist provenance so the next update is fully checkout-independent (spec §8).
    python_version = (
        install_argv[install_argv.index("--python") + 1] if "--python" in install_argv else "3.12"
    )
    metadata_persisted = True
    metadata_error: str | None = None
    try:
        metadata_writer(
            InstallMetadata(
                manager="uv-tool",
                source="official-github-vcs",
                repository=OFFICIAL_REPOSITORY,
                channel=plan.channel or OpenAgentUpdateChannel.CANDIDATE,
                channel_ref=plan.channel_ref,
                installed_version=verified,
                installed_commit=installed_commit,
                last_accepted_version=verified,
                last_accepted_commit=installed_commit,
                python=python_version,
            ),
            environ=environment,
        )
    except Exception as exc:  # noqa: BLE001 - any writer failure must be reported, not swallowed
        # The binary is installed and verified, so this is not a failed update. But it is not an
        # ordinary success either: without install.json this installation cannot say which channel
        # it follows or which commit it last accepted, so the next update has to fail closed. The
        # exception *class* is reported; its message may contain arbitrary local paths.
        metadata_persisted = False
        metadata_error = exc.__class__.__name__

    channel_name = plan.channel.value if plan.channel else "candidate"
    revised = plan.model_copy(
        update={
            "current_version": verified,
            "installed_commit": installed_commit,
            "update_available": False,
            "reason": "updated",
            "detail": f"updated to {verified} on the {channel_name} channel and verified",
        }
    )
    success_detail = (
        f"updated to {verified} (commit {installed_commit[:12]}) on the {channel_name} "
        "channel; exact executable, commit, and Doctor verified"
    )
    if not metadata_persisted:
        success_detail = (
            f"OpenAgent was updated to {verified} (commit {installed_commit[:12]}) successfully, "
            f"but update-channel metadata could not be persisted ({metadata_error}). This "
            f"installation can no longer prove its channel or last accepted commit, so the next "
            f"update will refuse to run until it is restored. Run: openagent update --repair"
        )
    return _vcs_result(
        revised,
        ok=True,
        ran=True,
        verified_version=verified,
        verified_commit=installed_commit,
        doctor_exit_code=doctor.returncode,
        backup_path=backup,
        metadata_persisted=metadata_persisted,
        status="installed" if metadata_persisted else "installed_with_metadata_warning",
        detail=success_detail,
    )
