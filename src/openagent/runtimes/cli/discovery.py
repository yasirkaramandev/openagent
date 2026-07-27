"""The full CLI discovery system (spec §19).

Six CLIs, and until now each one answered "what is installed and what can it do" its own way. That is
fine while the answers are only read by the adapter that produced them, and it stops being fine the
moment Doctor and the wizard have to render them side by side — at which point one adapter's silence
means "not installed", another's means "we did not check", and nothing distinguishes them.

So discovery produces one shape (:class:`CliDiscoveryReport`) via one flow, for every CLI, and the
descriptor (:class:`CliDescriptor`) says what to expect from each. Two things about the flow matter more
than the rest.

**Status is a sentence about evidence, not about quality.** The vocabulary in :class:`DiscoveryStatus`
distinguishes "verified live", "fixture validated", "installed but this version is unverified",
"installed but authentication unknown", "not installed" and "experimental". Collapsing the middle four
into "installed" is what makes a wizard confidently offer a CLI that will fail.

**Discovery must not be able to run the project's code.** Probing a CLI means executing it, and a
CLI executed inside a project directory reads that project's configuration — hooks, extensions, MCP
servers, agent definitions. So probes run from an empty directory with a minimal environment, and the
only credentials in scope are the ones that CLI documents. A Gemini probe never receives
``ANTHROPIC_API_KEY``, and the test for that is not optional.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ...core.models import CliInstallation, CliInstallSource
from ...security.process import minimal_environment
from .base import CliModelDiscoveryContext
from .registry import (
    EXPERIMENTAL,
    FIRST_CLASS,
    build_cli_adapter,
    cli_display_name,
    cli_status_label,
    known_cli_types,
)


class DiscoveryStatus(str, Enum):
    """The honest label vocabulary (spec §19.3).

    Six values because there are six genuinely different situations, and the four in the middle are
    the ones that get collapsed into "installed" by anyone writing this once per adapter.
    """

    VERIFIED_LIVE = "verified-live"
    FIXTURE_VALIDATED = "fixture-validated"
    VERSION_UNVERIFIED = "installed-version-unverified"
    AUTH_UNKNOWN = "installed-auth-unknown"
    UNSUPPORTED = "installed-unsupported"
    NOT_INSTALLED = "not-installed"
    EXPERIMENTAL = "experimental"

    @property
    def usable(self) -> bool:
        """Whether an agent could be created against this CLI right now.

        ``AUTH_UNKNOWN`` counts as usable: an undetectable interactive login is common, and blocking
        on that guess is worse than letting the CLI report its own error.
        """

        return self not in {DiscoveryStatus.NOT_INSTALLED, DiscoveryStatus.UNSUPPORTED}


class OutputProtocol(str, Enum):
    """How a CLI reports what it did."""

    #: One JSON object per line, streamed while the run proceeds.
    STREAM_JSON = "stream-json"
    #: One JSON document, printed when the run finishes.
    SINGLE_JSON = "single-json"
    #: Bidirectional JSON-RPC over stdio.
    JSON_RPC = "json-rpc"
    #: Human-readable text only.
    TEXT = "text"
    UNKNOWN = "unknown"


class ResumeStrategy(str, Enum):
    #: The CLI takes a session id on the command line.
    SESSION_FLAG = "session-flag"
    #: A protocol method loads a previous session.
    PROTOCOL_LOAD = "protocol-load"
    #: No verified headless resume contract exists. Not "unimplemented" — unproven (spec §11.5).
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class CliDescriptor:
    """What to expect from one CLI, as data (spec §19).

    Every field is a *description*, not an assertion: ``resume_strategy`` says which mechanism to look
    for, and discovery decides whether the installed binary actually has it. That split is why a
    descriptor cannot make a false capability claim — it has no field that means "supported".
    """

    cli_type: str
    display_name: str
    #: Executable names to look for, in preference order.
    executable_names: tuple[str, ...]
    version_commands: tuple[tuple[str, ...], ...] = (("--version",),)
    #: Install mechanisms this CLI ships through. Discovery reports which one is actually in play, and
    #: the updater refuses to act on a provenance not in this list.
    install_methods: tuple[CliInstallSource, ...] = ()
    #: Credential environment variables this CLI documents. Names only, and the *only* names a probe
    #: of this CLI may receive (spec §19.2).
    auth_environment: tuple[str, ...] = ()
    output_protocol: OutputProtocol = OutputProtocol.UNKNOWN
    resume_strategy: ResumeStrategy = ResumeStrategy.UNSUPPORTED
    #: Whether the CLI offers a sandbox OpenAgent can ask for.
    sandbox_flag: str | None = None
    #: The version this adapter's event mapping was actually captured against, if any.
    validated_version: str | None = None
    experimental: bool = False
    notes: str = ""


DESCRIPTORS: dict[str, CliDescriptor] = {
    "codex": CliDescriptor(
        cli_type="codex",
        display_name="Codex CLI",
        executable_names=("codex",),
        install_methods=(
            CliInstallSource.NPM,
            CliInstallSource.NATIVE,
            CliInstallSource.STANDALONE_RELEASE,
            CliInstallSource.HOMEBREW_CASK,
        ),
        auth_environment=("OPENAI_API_KEY", "CODEX_API_KEY"),
        output_protocol=OutputProtocol.STREAM_JSON,
        resume_strategy=ResumeStrategy.SESSION_FLAG,
        sandbox_flag="--sandbox",
        validated_version="codex-cli 0.142.5",
    ),
    "claude": CliDescriptor(
        cli_type="claude",
        display_name="Claude Code",
        executable_names=("claude",),
        install_methods=(
            CliInstallSource.NPM,
            CliInstallSource.NATIVE,
            CliInstallSource.HOMEBREW_CASK,
        ),
        auth_environment=("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
        output_protocol=OutputProtocol.STREAM_JSON,
        resume_strategy=ResumeStrategy.SESSION_FLAG,
    ),
    "gemini": CliDescriptor(
        cli_type="gemini",
        display_name="Gemini CLI",
        executable_names=("gemini",),
        install_methods=(CliInstallSource.NPM, CliInstallSource.HOMEBREW_CASK),
        auth_environment=(
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_LOCATION",
        ),
        # One document at the end, so live deltas are not available and are not claimed (spec §11.3).
        output_protocol=OutputProtocol.SINGLE_JSON,
        resume_strategy=ResumeStrategy.UNSUPPORTED,
        sandbox_flag="--sandbox",
        experimental=True,
        notes="`--output-format json` exists in some releases and not others, so it is probed.",
    ),
    "antigravity": CliDescriptor(
        cli_type="antigravity",
        display_name="Antigravity",
        executable_names=("agy",),
        install_methods=(CliInstallSource.NATIVE, CliInstallSource.STANDALONE_RELEASE),
        auth_environment=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        output_protocol=OutputProtocol.STREAM_JSON,
        resume_strategy=ResumeStrategy.SESSION_FLAG,
        experimental=True,
    ),
    "qwen": CliDescriptor(
        cli_type="qwen",
        display_name="Qwen Code",
        executable_names=("qwen",),
        install_methods=(CliInstallSource.NPM,),
        auth_environment=("DASHSCOPE_API_KEY", "QWEN_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"),
        output_protocol=OutputProtocol.STREAM_JSON,
        resume_strategy=ResumeStrategy.SESSION_FLAG,
        sandbox_flag="--safe-mode",
        experimental=True,
        notes="A Gemini-CLI fork; every flag is verified against the installed binary before use.",
    ),
    "kimi": CliDescriptor(
        cli_type="kimi",
        display_name="Kimi (ACP)",
        executable_names=("kimi",),
        install_methods=(CliInstallSource.NPM,),
        auth_environment=("MOONSHOT_API_KEY", "KIMI_API_KEY"),
        output_protocol=OutputProtocol.JSON_RPC,
        resume_strategy=ResumeStrategy.PROTOCOL_LOAD,
        experimental=True,
        notes="Capabilities come from the agent's own ACP initialize handshake.",
    ),
}


def get_descriptor(cli_type: str) -> CliDescriptor | None:
    return DESCRIPTORS.get(cli_type)


@dataclass
class CliDiscoveryReport:
    """Everything discovery established about one CLI on this machine (spec §19.1)."""

    cli_type: str
    display_name: str
    descriptor: CliDescriptor | None
    status: DiscoveryStatus
    status_label: str

    installed: bool = False
    #: The executable PATH resolution actually picked.
    resolved_executable: str | None = None
    #: Other executables of the same name that PATH shadows. A real condition, not an error — but it
    #: is the explanation for a surprising version, so it is reported rather than dropped.
    shadowed_executables: list[str] = field(default_factory=list)
    version: str | None = None
    validated_version: str | None = None
    version_verified: bool = False
    install_source: str = "unknown"

    authenticated: bool | None = None
    auth_detail: str = ""
    auth_blocking: bool = False
    #: Credential variable *names* found in the environment. Never values.
    auth_environment_present: list[str] = field(default_factory=list)

    #: Whether the CLI can report machine-readable events at all.
    structured_events: bool = False
    #: Whether those events arrive *while the run proceeds*. A separate question, and conflating the
    #: two promises incremental output that never arrives (spec §11.3, §17.1).
    live_structured_events: bool = False
    output_protocol: OutputProtocol = OutputProtocol.UNKNOWN

    resumable: bool = False
    resume_strategy: ResumeStrategy = ResumeStrategy.UNSUPPORTED
    sandbox_available: bool = False
    permission_profiles: list[str] = field(default_factory=list)

    model_discovery_available: bool = False
    model_discovery_method: str = ""
    model_count: int = 0
    model_discovery_error: str | None = None

    update_state: str = "unknown"
    update_provider: str | None = None
    latest_version: str | None = None

    experimental: bool = False
    #: Non-fatal problems worth surfacing: a shadowed binary, a failed probe, an unverified version.
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe, secret-free projection for ``doctor --json``."""

        return {
            "cli_type": self.cli_type,
            "display_name": self.display_name,
            "status": self.status.value,
            "status_label": self.status_label,
            "installed": self.installed,
            "resolved_executable": self.resolved_executable,
            "shadowed_executables": list(self.shadowed_executables),
            "version": self.version,
            "validated_version": self.validated_version,
            "version_verified": self.version_verified,
            "install_source": self.install_source,
            "authenticated": self.authenticated,
            "auth_detail": self.auth_detail,
            "auth_blocking": self.auth_blocking,
            "auth_environment_present": list(self.auth_environment_present),
            "structured_events": self.structured_events,
            "live_structured_events": self.live_structured_events,
            "output_protocol": self.output_protocol.value,
            "resumable": self.resumable,
            "resume_strategy": self.resume_strategy.value,
            "sandbox_available": self.sandbox_available,
            "permission_profiles": list(self.permission_profiles),
            "model_discovery": {
                "available": self.model_discovery_available,
                "method": self.model_discovery_method,
                "count": self.model_count,
                "error": self.model_discovery_error,
            },
            "update": {
                "state": self.update_state,
                "provider": self.update_provider,
                "latest_version": self.latest_version,
            },
            "experimental": self.experimental,
            "warnings": list(self.warnings),
        }


def discovery_environment(descriptor: CliDescriptor | None) -> dict[str, str]:
    """The environment a probe of *this* CLI may see (spec §19.2).

    Built from :func:`minimal_environment`, which carries no credential at all, then re-adding only the
    variables this CLI documents. That is the whole point: a Gemini probe receives ``GEMINI_API_KEY``
    if the user has one and never receives ``ANTHROPIC_API_KEY``, so a probe cannot become a path by
    which one provider's credential reaches another provider's process.
    """

    env = minimal_environment()
    if descriptor is None:
        return env
    for name in descriptor.auth_environment:
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def scratch_directory() -> Path:
    """An empty directory to probe from, so no project configuration is in scope.

    A CLI executed inside a project reads that project's hooks, extensions and agent definitions, any
    of which can run code. Discovery is not the moment for that to happen.
    """

    return Path(tempfile.mkdtemp(prefix="openagent-cli-discovery-"))


async def discover_cli(
    cli_type: str,
    *,
    project_root: Path | None = None,
    include_models: bool = True,
    include_updates: bool = False,
) -> CliDiscoveryReport:
    """Run the full discovery flow for one CLI (spec §19.1).

    Every step is best-effort and independently failable: a CLI whose auth probe times out is still
    reported as installed with ``authenticated=None``, because "we could not tell" is a different and
    more useful answer than "not authenticated".

    ``include_updates`` is off by default because an update check makes a network request, and Doctor
    should be runnable offline.
    """

    descriptor = get_descriptor(cli_type)
    report = CliDiscoveryReport(
        cli_type=cli_type,
        display_name=cli_display_name(cli_type),
        descriptor=descriptor,
        status=DiscoveryStatus.NOT_INSTALLED,
        status_label=cli_status_label(cli_type),
        output_protocol=descriptor.output_protocol if descriptor else OutputProtocol.UNKNOWN,
        resume_strategy=descriptor.resume_strategy if descriptor else ResumeStrategy.UNSUPPORTED,
        experimental=cli_type in EXPERIMENTAL,
    )

    try:
        adapter = build_cli_adapter(cli_type)
    except KeyError as exc:
        report.warnings.append(str(exc))
        return report

    installation = await _safe(adapter.detect(), report, "detect")
    if installation is None:
        report.status = DiscoveryStatus.NOT_INSTALLED
        report.status_label = f"{report.display_name} is not installed"
        report.model_discovery_error = "the CLI is not installed"
        return report

    _record_installation(report, installation)
    await _record_auth(report, adapter, descriptor)
    await _record_capabilities(report, adapter)
    _record_permissions(report, cli_type, descriptor)
    if include_models:
        await _record_models(report, adapter, descriptor, project_root)
    if include_updates:
        await _record_update(report, adapter)

    report.status, report.status_label = _classify(report, cli_type)
    return report


def _record_installation(report: CliDiscoveryReport, installation: CliInstallation) -> None:
    report.installed = True
    report.version = installation.version
    report.validated_version = installation.validated_version
    report.version_verified = bool(installation.version_verified)
    report.install_source = installation.install_source.value
    report.resolved_executable = installation.resolved_executable or installation.executable
    report.shadowed_executables = list(installation.shadowed_executables)
    if report.shadowed_executables:
        report.warnings.append(
            f"{len(report.shadowed_executables)} other {report.cli_type} executable(s) are shadowed "
            f"by PATH; the version in use is the one at {report.resolved_executable}"
        )
    if installation.validated_version and not report.version_verified:
        report.warnings.append(
            f"this adapter's event mapping was captured against {installation.validated_version}; "
            f"{installation.version or 'the installed version'} is not the same build"
        )


async def _record_auth(
    report: CliDiscoveryReport, adapter: Any, descriptor: CliDescriptor | None
) -> None:
    status = await _safe(adapter.inspect_auth(), report, "inspect_auth")
    if status is None:
        report.authenticated = None
        report.auth_detail = "the authentication probe did not complete"
        return
    report.authenticated = status.authenticated
    report.auth_detail = status.detail
    report.auth_blocking = bool(status.blocking)
    # Names only. The values stay in the child environment; a report is rendered in Doctor output and
    # written to JSON.
    report.auth_environment_present = list(status.environment_names)
    if descriptor is not None:
        foreign = set(report.auth_environment_present) - set(descriptor.auth_environment)
        if foreign:
            report.warnings.append(
                f"credential variables not documented for {report.cli_type} were found: "
                f"{sorted(foreign)}"
            )


#: Protocols that deliver events *while the run proceeds*. A single JSON document at the end does not,
#: whatever else it contains — that is the distinction spec §11.3 forbids collapsing.
_LIVE_PROTOCOLS = {OutputProtocol.STREAM_JSON, OutputProtocol.JSON_RPC}


async def _record_capabilities(report: CliDiscoveryReport, adapter: Any) -> None:
    caps = await _safe(adapter.capabilities(), report, "capabilities")
    if caps is None:
        return
    report.structured_events = bool(caps.structured_events)
    report.resumable = bool(caps.resumable)
    report.experimental = report.experimental or bool(caps.experimental)

    # An adapter that distinguishes the two answers is authoritative; Gemini and Qwen do, because for
    # them it depends on the installed build. For the rest it follows from the protocol: stream-json
    # and JSON-RPC deliver events during the run, a single JSON document does not. Defaulting to
    # False for all of them would under-claim for Codex and Claude and attach a warning about output
    # arriving late to CLIs that stream it live — under-claiming is the safe direction but it is still
    # wrong, and a wrong warning trains people to ignore warnings.
    live = getattr(adapter, "live_structured_events", None)
    if live is None:
        live = report.structured_events and report.output_protocol in _LIVE_PROTOCOLS
    report.live_structured_events = bool(live)

    if report.structured_events and not report.live_structured_events:
        report.warnings.append(
            "this CLI reports machine-readable results but not incremental output; the run console "
            "will fill in when the run finishes"
        )
    if not report.resumable:
        report.resume_strategy = ResumeStrategy.UNSUPPORTED


def _record_permissions(
    report: CliDiscoveryReport, cli_type: str, descriptor: CliDescriptor | None
) -> None:
    from ...core.permissions import PROFILES

    report.permission_profiles = sorted(PROFILES)
    report.sandbox_available = bool(descriptor and descriptor.sandbox_flag)


async def _record_models(
    report: CliDiscoveryReport,
    adapter: Any,
    descriptor: CliDescriptor | None,
    project_root: Path | None,
) -> None:
    lister = getattr(adapter, "list_models", None)
    report.model_discovery_method = str(getattr(adapter, "model_discovery_method", "") or "")
    if lister is None:
        report.model_discovery_error = "this adapter has no model discovery"
        return

    scratch: Path | None = None
    try:
        # Probing from an empty directory unless a project was explicitly named: a CLI run inside a
        # project reads its configuration, and discovery is not the moment for that.
        root = project_root
        if root is None:
            scratch = scratch_directory()
            root = scratch
        context = CliModelDiscoveryContext(
            project_root=root,
            executable=report.resolved_executable,
            environment=discovery_environment(descriptor),
        )
        try:
            models = await asyncio.wait_for(_call_lister(lister, context), timeout=60)
        except (TimeoutError, asyncio.TimeoutError):
            report.model_discovery_error = "model discovery timed out"
            return
        except Exception as exc:  # noqa: BLE001 - a failure is reported, never a fabricated list
            report.model_discovery_error = f"{exc.__class__.__name__}: {exc}"[:200]
            return
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)

    result = getattr(adapter, "last_model_discovery", None)
    if result is not None:
        report.model_discovery_available = bool(result.available)
        report.model_discovery_method = result.method or report.model_discovery_method
        report.model_count = len(result.models)
        report.model_discovery_error = result.error
        return
    report.model_discovery_available = bool(models)
    report.model_count = len(models)
    if not models:
        report.model_discovery_error = (
            "this CLI could not enumerate models; type a model id, or leave it blank to use the "
            "CLI's own default"
        )


async def _call_lister(lister: Any, context: CliModelDiscoveryContext) -> list[str]:
    """Call ``list_models`` with a context only if it accepts one.

    Decided by inspecting the signature rather than by calling and catching ``TypeError`` — catching
    would swallow a genuine ``TypeError`` from inside a context-aware adapter and silently retry it
    without the context, turning a real bug into a quietly degraded result.
    """

    import inspect

    try:
        accepts_context = bool(inspect.signature(lister).parameters)
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        accepts_context = False
    return await (lister(context) if accepts_context else lister())


async def _record_update(report: CliDiscoveryReport, adapter: Any) -> None:
    checker = getattr(adapter, "check_update", None)
    if checker is None:
        return
    status = await _safe(checker(), report, "check_update")
    if status is None:
        return
    state = getattr(status, "state", None)
    report.update_state = getattr(state, "value", str(state or "unknown"))
    report.latest_version = getattr(status, "latest_version", None)
    report.update_provider = getattr(status, "update_method", None) or getattr(
        status, "check_method", None
    )


async def _safe(awaitable: Any, report: CliDiscoveryReport, step: str) -> Any:
    """Await one discovery step, recording a failure as a warning rather than aborting the flow.

    A CLI whose auth probe times out is still installed, and reporting nothing about it because one
    step failed loses the answers the other steps already found.
    """

    try:
        return await awaitable
    except Exception as exc:  # noqa: BLE001 - discovery is best-effort; the reason is surfaced
        report.warnings.append(f"{step} failed: {exc.__class__.__name__}: {exc}"[:200])
        return None


def _classify(report: CliDiscoveryReport, cli_type: str) -> tuple[DiscoveryStatus, str]:
    """Choose the status label, most-specific first (spec §19.3).

    The order matters: a CLI can be simultaneously experimental, on an unverified version, and of
    unknown auth state, and reporting the *least* specific of those would hide the reason a user needs.
    """

    if not report.installed:
        return DiscoveryStatus.NOT_INSTALLED, f"{report.display_name} is not installed"

    if report.auth_blocking:
        return (
            DiscoveryStatus.UNSUPPORTED,
            f"installed, but not usable: {report.auth_detail or 'authentication is required'}",
        )

    if report.validated_version and not report.version_verified:
        return (
            DiscoveryStatus.VERSION_UNVERIFIED,
            f"installed but this version is unverified (validated against "
            f"{report.validated_version}, detected {report.version or 'unknown'})",
        )

    if report.authenticated is None:
        return (
            DiscoveryStatus.AUTH_UNKNOWN,
            "installed, but the authentication state could not be determined",
        )

    if cli_type in EXPERIMENTAL:
        return (
            DiscoveryStatus.EXPERIMENTAL,
            f"experimental: {cli_status_label(cli_type)}",
        )

    if cli_type in FIRST_CLASS and report.version_verified:
        return DiscoveryStatus.VERIFIED_LIVE, cli_status_label(cli_type)

    return DiscoveryStatus.FIXTURE_VALIDATED, cli_status_label(cli_type)


async def discover_all(
    *,
    project_root: Path | None = None,
    include_models: bool = True,
    include_updates: bool = False,
) -> list[CliDiscoveryReport]:
    """Discover every registered CLI, concurrently.

    Concurrent because these are independent subprocess probes and running six sequentially makes
    Doctor feel broken. Each one is individually bounded, so one hanging CLI cannot stall the rest.
    """

    results = await asyncio.gather(
        *(
            discover_cli(
                cli_type,
                project_root=project_root,
                include_models=include_models,
                include_updates=include_updates,
            )
            for cli_type in known_cli_types()
        ),
        return_exceptions=True,
    )
    reports: list[CliDiscoveryReport] = []
    for cli_type, result in zip(known_cli_types(), results, strict=False):
        if isinstance(result, CliDiscoveryReport):
            reports.append(result)
            continue
        # A discovery that raised at the top level still gets a row: an absent row reads as "we did
        # not look", which is the one thing this module exists to avoid.
        descriptor = get_descriptor(cli_type)
        reports.append(
            CliDiscoveryReport(
                cli_type=cli_type,
                display_name=cli_display_name(cli_type),
                descriptor=descriptor,
                status=DiscoveryStatus.NOT_INSTALLED,
                status_label=f"discovery failed: {result.__class__.__name__}",
                warnings=[str(result)[:200]],
            )
        )
    return reports
