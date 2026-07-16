"""Agent management + OPENAGENT.md sync (spec §3.3, §33)."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..core.models import (
    AgentProfile,
    AgentRuntime,
    ModelCapabilities,
    ModelVerification,
    RuntimeType,
)
from ..core.permissions import get_profile
from ..reporting.openagent_md import write_openagent_md

if TYPE_CHECKING:
    from ..app import OpenAgentApp


class AgentError(ValueError):
    pass


def _require_str(value: object, what: str) -> str:
    """Return ``value`` as a non-empty ``str`` or raise :class:`AgentError`.

    The second boundary against Textual Select sentinels (``Select.NULL``/``NoSelection``) and any
    other non-string reaching the service layer — see ``tui/select_utils.py``.
    """

    if not isinstance(value, str) or not value.strip():
        raise AgentError(what)
    return value.strip()


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
        max_steps: int = 40,
        reasoning_effort: str | None = None,
        model_override_reason: str | None = None,
    ) -> AgentProfile:
        # Reject non-string bindings *before* Pydantic so a leaked Textual sentinel (Select.NULL)
        # or any other non-string never reaches AgentRuntime and blows up with a raw ValidationError.
        name = _require_str(name, "agent name is required")
        get_profile(permission_profile)  # validate
        verification: ModelVerification | None = None
        if runtime_type is RuntimeType.API_AGENT:
            provider = _require_str(provider, "API agent requires a valid provider connection")
            model = _require_str(model, "API agent requires a valid model id")
            cli = None
            # Fail closed on a dangling reference: an API agent must point at a provider that
            # actually exists, so a run never dies later with "provider not found" (item 7).
            if self.app.providers.get(provider) is None:
                raise AgentError(f"provider {provider!r} does not exist")
            connection = self.app.providers.get(provider)
            assert connection is not None
            probe = self.app.providers.cached_probe(provider, model)
            verified = bool(probe and probe.agent_compatible and probe.category == "verified")
            reason = (model_override_reason or "").strip() or None
            verification = ModelVerification(
                status="verified" if verified else "overridden" if reason else "unverified",
                verified_at=probe.tested_at if verified and probe else None,
                probe_version=probe.probe_version if probe else None,
                capability_snapshot=probe.capabilities if probe else ModelCapabilities(),
                override_reason=reason,
                provider_fingerprint=_fingerprint(
                    f"{connection.id}|{connection.base_url}|{connection.protocol.value}|"
                    f"{connection.credential_revision}"
                ),
                model_fingerprint=_fingerprint(model),
            )
        elif runtime_type is RuntimeType.CLI:
            cli = _require_str(cli, "CLI agent requires a valid CLI selection")
            provider = None
            # A CLI agent MAY pin a model (``codex -m`` / ``claude --model``). It is optional —
            # ``None`` means "use the CLI's own configured default" — but it is not meaningless:
            # without it the agent inherits whatever ``~/.codex/config.toml`` names, which may be a
            # model the installed CLI cannot run at all (observed live: "requires a newer version
            # of Codex"). Pinning it makes the agent reproducible instead of dependent on global
            # config the user may not even remember setting.
            model = (model or "").strip() or None
        if self.repos.agents.get(name):
            raise AgentError(f"agent {name!r} already exists")

        agent = AgentProfile(
            name=name,
            title=title,
            description=description,
            runtime=AgentRuntime(
                type=runtime_type,
                provider=provider,
                model=model,
                cli=cli,
                reasoning_effort=reasoning_effort,
                model_verification=verification,
            ),
            tags=tags or [],
            system_prompt=system_prompt,
            permission_profile=permission_profile,
            max_steps=max_steps,
        )
        operation = self.app.journal.begin(
            "agent_document_sync", {"path": str(self.app.paths.openagent_md())}
        )
        self.repos.agents.upsert(agent)
        operation.advance("db_written")
        try:
            self.sync_openagent_md()
        except Exception:
            # Keep the agent row and OPENAGENT.md consistent: if the doc write fails, don't leave a
            # persisted agent the file doesn't mention (item 3).
            self.repos.agents.delete(name)
            operation.complete()
            raise
        operation.complete()
        return agent

    def create_with_new_provider(
        self,
        *,
        provider_name: str,
        provider_type: str,
        model: str,
        api_key: str | None = None,
        key_env: str | None = None,
        credential_source: str | None = None,
        protocol: object = None,
        base_url: str | None = None,
        anthropic_base_url: str | None = None,
        region: str | None = None,
        workspace_id: str | None = None,
        extra_headers: dict[str, str] | None = None,
        **agent_fields: object,
    ) -> AgentProfile:
        """Atomically connect a new provider *and* create an agent that binds it (item 3).

        Order and rollback:

        1. Pre-validate the agent (name/model/profile, name uniqueness) **before** any write, so an
           invalid agent never creates a dangling provider.
        2. Open a provider transaction (credential validated + fail-closed inside
           ``ProviderService.create_transaction``): the secret + provider row are written now.
        3. Create the agent, then ``commit()``. If agent creation fails for *any* reason — duplicate
           name slipping through, an OPENAGENT.md write error, a repository error — the transaction's
           ``__exit__`` rolls back the provider row and restores the keychain exactly, so the system
           is left as it started. The previous secret lives only on the transaction stack (§6), never
           in a long-lived service field.
        """

        from ..core.models import Protocol  # local import avoids widening the module surface

        agent_name = _require_str(agent_fields.get("name"), "agent name is required")
        _require_str(model, "API agent requires a valid model id")
        get_profile(str(agent_fields.get("permission_profile") or "safe-edit"))
        if self.repos.agents.get(agent_name):
            raise AgentError(f"agent {agent_name!r} already exists")
        if self.app.providers.get(provider_name):
            raise AgentError(f"a provider named {provider_name!r} already exists")

        with self.app.providers.create_transaction(
            name=provider_name,
            provider_type=provider_type,
            protocol=protocol if isinstance(protocol, Protocol) else None,
            base_url=base_url,
            anthropic_base_url=anthropic_base_url,
            api_key=api_key,
            key_env=key_env,
            credential_source=credential_source,
            region=region,
            workspace_id=workspace_id,
            extra_headers=extra_headers,
        ) as tx:
            agent = self.create(
                runtime_type=RuntimeType.API_AGENT,
                provider=provider_name,
                model=model,
                **agent_fields,  # type: ignore[arg-type]
            )
            tx.commit()  # provider AND agent are durable — forget the previous secret (§6)
            return agent

    def update(
        self,
        name: str,
        *,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        system_prompt: str | None = None,
        permission_profile: str | None = None,
    ) -> AgentProfile:
        """Update mutable fields of an existing agent (runtime/name are immutable)."""

        agent = self.repos.agents.get(name)
        if not agent:
            raise AgentError(f"agent {name!r} not found")
        if permission_profile is not None:
            get_profile(permission_profile)  # validate
        updates = {
            k: v
            for k, v in {
                "title": title,
                "description": description,
                "tags": tags,
                "system_prompt": system_prompt,
                "permission_profile": permission_profile,
            }.items()
            if v is not None
        }
        updated = agent.model_copy(update=updates)
        operation = self.app.journal.begin(
            "agent_document_sync", {"path": str(self.app.paths.openagent_md())}
        )
        self.repos.agents.upsert(updated)
        operation.advance("db_written")
        try:
            self.sync_openagent_md()
        except Exception:
            self.repos.agents.upsert(agent)
            operation.complete()
            raise
        operation.complete()
        return updated

    def list(self) -> Sequence[AgentProfile]:
        return self.repos.agents.list()

    def get(self, name: str) -> AgentProfile | None:
        return self.repos.agents.get(name)

    def remove(self, name: str) -> bool:
        existing = self.repos.agents.get(name)
        if existing is None:
            return False
        operation = self.app.journal.begin(
            "agent_document_sync", {"path": str(self.app.paths.openagent_md())}
        )
        removed = self.repos.agents.delete(name)
        if removed:
            operation.advance("db_deleted")
            try:
                self.sync_openagent_md()
            except Exception:
                self.repos.agents.upsert(existing)
                operation.complete()
                raise
        operation.complete()
        return removed

    def sync_openagent_md(self) -> None:
        write_openagent_md(self.app.paths.openagent_md(), self.list())


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
