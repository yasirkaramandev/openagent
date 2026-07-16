"""Run preflight — prove an agent can actually run *before* starting it (item 7).

The old flow discovered a missing CLI, a missing login, or an empty model only once the run had a
directory, a worktree, an id, and a half-written event log. Preflight moves those checks to the front
and reports them as a checklist the user can act on::

    ✓ Agent exists
    ✓ Codex found: /opt/homebrew/bin/codex
    ✓ Version: codex-cli 0.142.5
    ✓ Authentication detected
    ✓ codex exec supports --json
    ✓ Sandbox 'workspace-write' supported
    ✓ Permission profile: safe-edit
    ✓ Workspace exists

A failed **mandatory** check blocks the run. A failed optional check is a warning (version detection
is best-effort; a CLI that hides its version is still usable). Every check here is local/offline —
network connection testing stays opt-in, because it costs a request; missing *local* configuration
blocks immediately.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.models import AgentProfile, CredentialType, RuntimeType
from ..core.permissions import PROFILES, get_profile
from ..credentials.redaction import redact, secret_scope
from ..providers.factory import build_adapter, resolve_base_url
from ..runtimes.cli.antigravity import AntigravityAdapter
from ..runtimes.cli.base import detect_version, find_executable
from ..runtimes.cli.registry import build_cli_adapter, known_cli_types

if TYPE_CHECKING:
    from ..app import OpenAgentApp


@dataclass
class Check:
    """One preflight assertion."""

    name: str
    ok: bool
    detail: str = ""
    #: A failed mandatory check blocks the run; a failed optional check is only a warning.
    mandatory: bool = True
    #: The normalized failure type this check produces when it blocks a run (item 13).
    error_type: str = "preflight_failed"

    @property
    def blocking(self) -> bool:
        return self.mandatory and not self.ok

    @property
    def symbol(self) -> str:
        if self.ok:
            return "✓"
        return "✗" if self.mandatory else "!"

    def line(self) -> str:
        detail = f": {self.detail}" if self.detail else ""
        return f"{self.symbol} {self.name}{detail}"


@dataclass
class PreflightReport:
    checks: list[Check] = field(default_factory=list)

    def add(
        self,
        name: str,
        ok: bool,
        detail: str = "",
        *,
        mandatory: bool = True,
        error_type: str = "preflight_failed",
    ) -> Check:
        check = Check(name=name, ok=ok, detail=detail, mandatory=mandatory, error_type=error_type)
        self.checks.append(check)
        return check

    @property
    def error_type(self) -> str:
        """The failure type of the first blocking check — what the run is recorded as failing with."""

        return self.blockers[0].error_type if self.blockers else "preflight_failed"

    @property
    def ok(self) -> bool:
        """True when nothing mandatory failed — the only gate that may start a run."""
        return not any(c.blocking for c in self.checks)

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if c.blocking]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and not c.mandatory]

    def summary(self) -> str:
        return "; ".join(c.detail or c.name for c in self.blockers) or "preflight passed"

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [
                {"name": c.name, "ok": c.ok, "detail": c.detail, "mandatory": c.mandatory}
                for c in self.checks
            ],
        }


class PreflightService:
    """Runs the readiness checklist for an agent + permission profile + workspace."""

    def __init__(self, app: OpenAgentApp) -> None:
        self.app = app

    async def check(
        self,
        *,
        agent_name: str,
        permission_profile: str | None = None,
        workspace: Path | None = None,
    ) -> PreflightReport:
        report = PreflightReport()
        agent = self.app.repos.agents.get(agent_name)
        if agent is None:
            report.add(
                "Agent exists",
                False,
                f"no agent named {agent_name!r}",
                error_type="agent_not_found",
            )
            return report
        report.add("Agent exists", True, agent.name)

        profile_name = permission_profile or agent.permission_profile
        if profile_name not in PROFILES:
            report.add(
                "Permission profile",
                False,
                f"unknown profile {profile_name!r}",
                error_type="permission_profile_invalid",
            )
            return report
        report.add("Permission profile", True, profile_name)

        root = workspace or self.app.paths.project_root
        report.add("Workspace exists", root.is_dir(), str(root), error_type="workspace_failed")

        rtype = agent.runtime.type
        if rtype in (RuntimeType.CLI, RuntimeType.CLI.value):
            await self._check_cli(report, agent, profile_name)
        else:
            self._check_api(report, agent)
        return report

    # ------------------------------------------------------------------ CLI

    async def _check_cli(
        self, report: PreflightReport, agent: AgentProfile, profile_name: str
    ) -> None:
        cli_type = agent.runtime.cli or ""
        if cli_type not in known_cli_types():
            report.add(
                "CLI type is known",
                False,
                f"{cli_type!r} is not a known CLI (known: {', '.join(known_cli_types())})",
                error_type="cli_not_found",
            )
            return
        report.add("CLI type is known", True, cli_type)

        adapter = build_cli_adapter(cli_type)
        executable = getattr(adapter, "executable", None)
        if not executable:
            report.add(
                f"{cli_type} is installed",
                False,
                f"{cli_type} was not found on PATH — install it, then re-run readiness",
                error_type="cli_not_found",
            )
            return
        report.add(f"{cli_type} found", True, executable)

        path = Path(executable)
        report.add(
            "Executable is runnable",
            path.is_file() and os.access(path, os.X_OK),
            str(path),
            error_type="cli_not_found",
        )

        version = detect_version(executable)
        report.add(
            "Version detected",
            bool(version),
            version or "could not read --version",
            mandatory=False,
        )

        try:
            auth = await adapter.inspect_auth()
            report.add(
                "Authentication detected",
                bool(auth.authenticated),
                auth.detail,
                error_type="authentication_failed",
            )
        except Exception as exc:  # noqa: BLE001 - auth probing is best-effort
            report.add(
                "Authentication detected",
                False,
                f"could not check auth: {exc}",
                error_type="authentication_failed",
            )

        if cli_type == "codex":
            self._check_codex(report, executable, profile_name)
        elif cli_type == "claude":
            self._check_claude(report, executable, agent)
        elif cli_type == "antigravity":
            allowed, reason = adapter.permission_status(profile_name)  # type: ignore[attr-defined]
            report.add(
                "Adapter supports the requested mode",
                allowed,
                reason,
                error_type="permission_mode_unsupported",
            )
        else:
            profile = get_profile(profile_name)
            caps = await adapter.capabilities()
            supported = caps.edits_files or not profile.can_edit_files
            report.add(
                "Adapter supports the requested mode",
                supported,
                f"{profile_name} on {cli_type}",
                error_type="permission_mode_unsupported",
            )

    def _check_codex(self, report: PreflightReport, executable: str, profile_name: str) -> None:
        """Codex-specific readiness: ``codex exec``, JSON output, and the sandbox we will request."""

        help_text = _exec_help(executable)
        if help_text is None:
            report.add(
                "codex exec is available",
                False,
                "`codex exec --help` did not run — the CLI may be broken or too old",
                error_type="cli_not_found",
            )
            return
        report.add("codex exec is available", True, "`codex exec --help` ok")
        report.add(
            "codex exec supports --json",
            "--json" in help_text,
            "JSONL event stream"
            if "--json" in help_text
            else "this codex build has no --json output; OpenAgent needs it for live events",
            error_type="schema_mismatch",
        )

        sandbox = get_profile(profile_name).codex_sandbox
        # `codex exec --help` lists the accepted --sandbox values; only claim support for a real one.
        supported = sandbox in help_text
        report.add(
            f"Sandbox '{sandbox}' supported",
            supported,
            f"{profile_name} → --sandbox {sandbox}"
            if supported
            else f"this codex build does not accept --sandbox {sandbox}",
            error_type="permission_mode_unsupported",
        )

    def _check_claude(self, report: PreflightReport, executable: str, agent: AgentProfile) -> None:
        help_text = _root_help(executable)
        if help_text is None:
            report.add(
                "Claude CLI help is available",
                False,
                "`claude --help` did not run; model/effort support cannot be verified",
                error_type="cli_version_unsupported",
            )
            return
        requested_model = agent.runtime.model
        requested_effort = agent.runtime.reasoning_effort
        report.add(
            "Claude model flag is supported",
            not requested_model or "--model" in help_text,
            requested_model or "CLI default model",
            error_type="cli_version_unsupported",
        )
        report.add(
            "Claude effort flag is supported",
            not requested_effort or "--effort" in help_text,
            requested_effort or "CLI default effort",
            error_type="cli_version_unsupported",
        )

    # ------------------------------------------------------------------ API

    def _check_api(self, report: PreflightReport, agent: AgentProfile) -> None:
        name = agent.runtime.provider or ""
        provider = self.app.repos.providers.get_by_name(name)
        if provider is None:
            report.add(
                "Provider exists",
                False,
                f"agent points at provider {name!r}, which is not registered",
                error_type="provider_not_found",
            )
            return
        report.add("Provider exists", True, f"{provider.name} ({provider.provider_type})")

        cred = provider.credential
        ctype = cred.type if isinstance(cred.type, CredentialType) else CredentialType(cred.type)
        valid, why = _credential_ref_valid(ctype, cred)
        report.add("Credential reference is valid", valid, why, error_type="credential_invalid")
        if not valid:
            return

        if ctype is CredentialType.NONE:
            report.add("Credential is available", True, "provider needs no key")
        else:
            try:
                available = self.app.credentials.available(cred)
                detail = (
                    f"{ctype.value} credential resolved"
                    if available
                    else f"{ctype.value} credential is missing — re-add the provider's key"
                )
            except Exception as exc:  # noqa: BLE001 - a broken keychain must not crash preflight
                available, detail = False, f"could not read credential: {exc}"
            report.add(
                "Credential is available", available, detail, error_type="credential_missing"
            )

        try:
            base_url = resolve_base_url(provider)
            report.add("Base URL resolves", True, base_url)
        except ValueError as exc:
            report.add("Base URL resolves", False, str(exc), error_type="base_url_invalid")
            return

        model = (agent.runtime.model or "").strip()
        report.add(
            "Model is set",
            bool(model),
            model or "the agent has no model id",
            error_type="model_missing",
        )

        key = self.app.credentials.resolve(cred)
        with secret_scope(key):
            try:
                build_adapter(provider, key)
                report.add("Provider adapter builds", True, provider.protocol.value)
            except Exception as exc:  # noqa: BLE001 - construction failure is a blocker
                report.add(
                    "Provider adapter builds",
                    False,
                    redact(str(exc)),
                    error_type="provider_adapter_failed",
                )


# --------------------------------------------------------------------------- helpers


def _credential_ref_valid(ctype: CredentialType, cred) -> tuple[bool, str]:
    if ctype is CredentialType.ENV:
        if not cred.env_var:
            return False, "env credential has no variable name"
        return True, f"env var {cred.env_var}"
    if ctype is CredentialType.EXTERNAL_COMMAND:
        if not cred.command:
            return False, "external-command credential has no command"
        return True, "external command"
    if ctype is CredentialType.KEYCHAIN:
        if not cred.account:
            return False, "keychain credential has no account"
        return True, f"keychain {cred.service}/{cred.account}"
    if ctype is CredentialType.SESSION:
        return True, "session credential"
    return True, "no credential required"


#: ``codex exec --help`` is stable and cheap; cache it per executable for the life of the process.
_HELP_CACHE: dict[str, str | None] = {}


def _exec_help(executable: str) -> str | None:
    if executable in _HELP_CACHE:
        return _HELP_CACHE[executable]
    try:
        result = subprocess.run(  # noqa: S603 - executable came from our own PATH lookup
            [executable, "exec", "--help"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        text = (result.stdout or "") + (result.stderr or "")
        _HELP_CACHE[executable] = text if result.returncode == 0 and text else None
    except (OSError, subprocess.TimeoutExpired):
        _HELP_CACHE[executable] = None
    return _HELP_CACHE[executable]


def _root_help(executable: str) -> str | None:
    key = f"{executable}::__root__"
    if key not in _HELP_CACHE:
        try:
            result = subprocess.run(
                [executable, "--help"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            _HELP_CACHE[key] = result.stdout + result.stderr if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            _HELP_CACHE[key] = None
    return _HELP_CACHE[key]


def antigravity_permission_status(profile_name: str) -> tuple[bool, str]:
    """Public helper for Doctor/wizard: is this profile runnable on Antigravity right now?"""

    return AntigravityAdapter().permission_status(profile_name)


def find_cli_executable(cli_type: str) -> str | None:
    return find_executable(cli_type)
