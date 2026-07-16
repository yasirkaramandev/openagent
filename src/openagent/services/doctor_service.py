"""System diagnostics (spec §41).

``openagent doctor`` runs local, offline checks: config/DB/keychain/git health, which CLIs are
installed and whether they look authenticated, configured providers, and OPENAGENT.md sync. Live
provider network tests are intentionally excluded so doctor stays fast and CI-safe.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..core.models import CredentialType, RuntimeType
from ..credentials.store import keychain_available
from ..providers.factory import get_preset
from ..reporting.openagent_md import render_agents_block
from ..runtimes.cli.registry import (
    cli_install_status,
    cli_registry_entries,
    known_cli_types,
)
from ..workspaces.worktree import is_git_repo

if TYPE_CHECKING:
    from ..app import OpenAgentApp

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


class DoctorService:
    def __init__(self, app: OpenAgentApp) -> None:
        self.app = app

    async def run(self) -> list[Check]:
        checks: list[Check] = []
        checks.append(Check("OpenAgent configuration", OK, str(self.app.paths.config_dir)))
        checks.append(
            Check(
                "SQLite writable",
                OK if self.app.db.writable() else FAIL,
                str(self.app.paths.db_path),
            )
        )
        checks.append(
            Check(
                "OS keychain available",
                OK if keychain_available() else WARN,
                "keychain backend present"
                if keychain_available()
                else "no usable keychain backend",
            )
        )
        git = shutil.which("git")
        checks.append(Check("Git installed", OK if git else FAIL, git or "git not found"))
        is_repo = is_git_repo(self.app.paths.project_root)
        checks.append(
            Check(
                "Current directory is a Git repository",
                OK if is_repo else WARN,
                "worktree isolation available"
                if is_repo
                else "non-git: runs use lower-safety copies",
            )
        )

        checks.extend(await self._cli_checks())
        checks.extend(self._provider_checks())
        checks.extend(self._agent_checks())
        checks.append(self._openagent_md_check())
        return checks

    async def _cli_checks(self) -> list[Check]:
        """Per-CLI readiness for every known runtime — Codex, Claude Code, and Antigravity (item 18).

        For each installed CLI, distinguishes: executable detected, authentication detected,
        structured-output/resume support, and the adapter's honest verification status (spec §17).
        A binary being present is never reported as "ready" on its own.
        """

        checks: list[Check] = []
        for entry in await cli_registry_entries():
            name = entry.display_name
            if not entry.installed:
                checks.append(Check(f"{name} installed", WARN, "not found"))
                continue
            checks.append(
                Check(
                    f"{name} installed",
                    OK,
                    entry.version or entry.executable or "detected",
                )
            )
            checks.append(
                Check(
                    f"{name} authentication",
                    OK if entry.authenticated else WARN,
                    entry.auth_detail
                    or ("authenticated" if entry.authenticated else "not detected"),
                )
            )
            caps = (
                f"structured output: {'yes' if entry.structured_events else 'no'}, "
                f"resume: {'yes' if entry.resumable else 'no'}"
                f"{' (experimental)' if entry.experimental else ''}"
            )
            # An adapter validated against one version cannot claim "verified" on another (item 16).
            verified = entry.version_verified or not entry.validated_version
            checks.append(
                Check(
                    f"{name} adapter status",
                    OK if verified else WARN,
                    f"{entry.status_label}; {caps}",
                )
            )
            if entry.type == "antigravity":
                checks.append(self._antigravity_permission_check())
        return checks

    def _antigravity_permission_check(self) -> Check:
        """What Antigravity is actually allowed to do right now, and why (item 15)."""

        from .preflight import antigravity_permission_status

        edit_ok, reason = antigravity_permission_status("safe-edit")
        if not edit_ok:
            return Check(
                "antigravity permissions",
                OK,
                "read-only (supported). Editing is experimental and OFF: a non-interactive "
                "--print run can only edit with --dangerously-skip-permissions, which disables "
                "Antigravity's own tool checks. Set OPENAGENT_ANTIGRAVITY_EXPERIMENTAL_EDIT=1 to "
                "opt in.",
            )
        return Check(
            "antigravity permissions",
            WARN,
            f"editing ENABLED — Antigravity's native permission checks are bypassed ({reason})",
        )

    def _provider_checks(self) -> list[Check]:
        providers = self.app.repos.providers.list()
        if not providers:
            return [Check("Providers configured", WARN, "no API providers added yet")]
        checks: list[Check] = [
            Check("Providers configured", OK, ", ".join(p.name for p in providers))
        ]
        for p in providers:
            checks.append(self._provider_credential_check(p))
        return checks

    def _provider_credential_check(self, provider) -> Check:
        """Offline credential health for one provider (item 20) — no network call."""

        name = f"Credential: {provider.name}"
        cred = provider.credential
        preset = get_preset(provider.provider_type)
        needs_key = preset.needs_key if preset else True

        if cred.type is CredentialType.ENV:
            if not cred.env_var:
                return Check(name, FAIL, "env credential has no variable name")
            if os.environ.get(cred.env_var) is None:
                return Check(name, WARN, f"env var {cred.env_var} is not set")
            return Check(name, OK, f"env var {cred.env_var} is set")
        if cred.type is CredentialType.KEYCHAIN:
            if not self.app.credentials.available(cred):
                sev = FAIL if needs_key else WARN
                return Check(name, sev, "no stored key in the keychain")
            return Check(name, OK, "key present in keychain")
        if cred.type is CredentialType.NONE:
            if needs_key:
                return Check(name, FAIL, "no credential but this provider type requires a key")
            return Check(name, OK, "no key required")
        return Check(name, OK, cred.type.value)

    def _agent_checks(self) -> list[Check]:
        agents = self.app.repos.agents.list()
        if not agents:
            return []
        provider_names = {p.name for p in self.app.repos.providers.list()}
        installed = {c for c, ok in cli_install_status() if ok}
        known = set(known_cli_types())
        checks: list[Check] = []
        for agent in agents:
            rt = agent.runtime
            rtype = rt.type if isinstance(rt.type, str) else rt.type.value
            label = f"Agent: {agent.name}"
            if rtype == RuntimeType.API_AGENT.value:
                if rt.provider not in provider_names:
                    checks.append(
                        Check(label, FAIL, f"references missing provider {rt.provider!r}")
                    )
                else:
                    checks.append(Check(label, OK, f"provider {rt.provider!r} present"))
            else:
                cli = rt.cli or ""
                if cli not in known:
                    checks.append(Check(label, WARN, f"unknown CLI runtime {cli!r}"))
                elif cli not in installed:
                    checks.append(Check(label, WARN, f"CLI {cli!r} is not installed"))
                else:
                    checks.append(Check(label, OK, f"CLI {cli!r} installed"))
        return checks

    def _openagent_md_check(self) -> Check:
        path = self.app.paths.openagent_md()
        agents = self.app.repos.agents.list()
        if not path.exists():
            return Check(
                "OPENAGENT.md synchronized",
                WARN if agents else OK,
                "not generated yet" if agents else "no agents to document",
            )
        expected = render_agents_block(agents).strip()
        synced = expected in path.read_text(encoding="utf-8")
        return Check(
            "OPENAGENT.md synchronized",
            OK if synced else WARN,
            "up to date" if synced else "stale; re-run `openagent add`/`remove`",
        )


def overall_ok(checks: list[Check]) -> bool:
    return all(c.status != FAIL for c in checks)
