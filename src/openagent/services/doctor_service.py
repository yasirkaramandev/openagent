"""System diagnostics (spec §41).

``openagent doctor`` runs local, offline checks: config/DB/keychain/git health, which CLIs are
installed and whether they look authenticated, configured providers, and OPENAGENT.md sync. Live
provider network tests are intentionally excluded so doctor stays fast and CI-safe.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..credentials.store import keychain_available
from ..reporting.openagent_md import render_agents_block
from ..runtimes.cli.registry import build_cli_adapter, discover_installed
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
        checks.append(Check(
            "SQLite writable", OK if self.app.db.writable() else FAIL, str(self.app.paths.db_path)
        ))
        checks.append(Check(
            "OS keychain available",
            OK if keychain_available() else WARN,
            "keychain backend present" if keychain_available() else "no usable keychain backend",
        ))
        git = shutil.which("git")
        checks.append(Check("Git installed", OK if git else FAIL, git or "git not found"))
        is_repo = is_git_repo(self.app.paths.project_root)
        checks.append(Check(
            "Current directory is a Git repository",
            OK if is_repo else WARN,
            "worktree isolation available" if is_repo else "non-git: runs use lower-safety copies",
        ))

        checks.extend(await self._cli_checks())
        checks.extend(self._provider_checks())
        checks.append(self._openagent_md_check())
        return checks

    async def _cli_checks(self) -> list[Check]:
        checks: list[Check] = []
        installed = {c.type: c for c in await discover_installed()}
        for cli_type in ("codex", "claude"):
            install = installed.get(cli_type)
            if install is None:
                checks.append(Check(f"{cli_type} CLI installed", WARN, "not found"))
                continue
            checks.append(Check(f"{cli_type} CLI installed", OK, install.version or install.executable))
            adapter = build_cli_adapter(cli_type, install.executable)
            auth = await adapter.inspect_auth()
            checks.append(Check(
                f"{cli_type} authentication", OK if auth.authenticated else WARN, auth.detail
            ))
        return checks

    def _provider_checks(self) -> list[Check]:
        providers = self.app.repos.providers.list()
        if not providers:
            return [Check("Providers configured", WARN, "no API providers added yet")]
        names = ", ".join(p.name for p in providers)
        return [Check("Providers configured", OK, names)]

    def _openagent_md_check(self) -> Check:
        path = self.app.paths.openagent_md()
        agents = self.app.repos.agents.list()
        if not path.exists():
            return Check("OPENAGENT.md synchronized", WARN if agents else OK,
                         "not generated yet" if agents else "no agents to document")
        expected = render_agents_block(agents).strip()
        synced = expected in path.read_text(encoding="utf-8")
        return Check("OPENAGENT.md synchronized", OK if synced else WARN,
                     "up to date" if synced else "stale; re-run `openagent add`/`remove`")


def overall_ok(checks: list[Check]) -> bool:
    return all(c.status != FAIL for c in checks)
