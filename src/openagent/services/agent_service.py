"""Agent management + OPENAGENT.md sync (spec §3.3, §33)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.models import AgentProfile, AgentRuntime, RuntimeType
from ..core.permissions import get_profile
from ..reporting.openagent_md import write_openagent_md

if TYPE_CHECKING:
    from ..app import OpenAgentApp


class AgentError(ValueError):
    pass


class AgentService:
    def __init__(self, app: OpenAgentApp) -> None:
        self.app = app
        self.repos = app.repos

    def create(
        self,
        *,
        name: str,
        title: str = "",
        description: str = "",
        runtime_type: RuntimeType,
        provider: str | None = None,
        model: str | None = None,
        cli: str | None = None,
        tags: list[str] | None = None,
        system_prompt: str = "",
        permission_profile: str = "safe-edit",
    ) -> AgentProfile:
        get_profile(permission_profile)  # validate
        if runtime_type is RuntimeType.API_AGENT and not (provider and model):
            raise AgentError("api-agent requires both --provider and --model")
        if runtime_type is RuntimeType.CLI and not cli:
            raise AgentError("cli agent requires --cli")
        if self.repos.agents.get(name):
            raise AgentError(f"agent {name!r} already exists")

        agent = AgentProfile(
            name=name, title=title, description=description,
            runtime=AgentRuntime(type=runtime_type, provider=provider, model=model, cli=cli),
            tags=tags or [], system_prompt=system_prompt, permission_profile=permission_profile,
        )
        self.repos.agents.upsert(agent)
        self.sync_openagent_md()
        return agent

    def list(self) -> list[AgentProfile]:
        return self.repos.agents.list()

    def get(self, name: str) -> AgentProfile | None:
        return self.repos.agents.get(name)

    def remove(self, name: str) -> bool:
        removed = self.repos.agents.delete(name)
        if removed:
            self.sync_openagent_md()
        return removed

    def sync_openagent_md(self) -> None:
        write_openagent_md(self.app.paths.openagent_md(), self.list())
