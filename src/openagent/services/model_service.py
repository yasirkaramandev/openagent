"""Model registration + capability probing (spec §25)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.models import ModelCapabilities, ModelProfile
from ..providers.discovery import apply_probe

if TYPE_CHECKING:
    from ..app import OpenAgentApp


class ModelService:
    def __init__(self, app: OpenAgentApp) -> None:
        self.app = app
        self.repos = app.repos

    def add(
        self,
        *,
        provider_name: str,
        remote_model_id: str,
        deployment_id: str | None = None,
        capabilities: ModelCapabilities | None = None,
    ) -> ModelProfile:
        provider = self.repos.providers.get_by_name(provider_name)
        if not provider:
            raise ValueError(f"provider {provider_name!r} not found")
        model = ModelProfile(
            id=f"model_{provider_name}_{remote_model_id}".replace("/", "_"),
            provider_connection=provider.id,
            remote_model_id=remote_model_id,
            deployment_id=deployment_id,
            capabilities=capabilities or ModelCapabilities(),
        )
        self.repos.models.upsert(model)
        return model

    def list_for_provider(self, provider_name: str) -> list[ModelProfile]:
        provider = self.repos.providers.get_by_name(provider_name)
        if not provider:
            return []
        return self.repos.models.list_for_provider(provider.id)

    async def probe(self, model_id: str) -> ModelProfile | None:
        model = self.repos.models.get(model_id)
        if not model:
            return None
        provider = self.repos.providers.get(model.provider_connection)
        if not provider:
            return None
        with self.app.providers.adapter_scope(provider) as adapter:
            try:
                probed = await adapter.probe_model(model.remote_model_id)
            finally:
                transport = getattr(adapter, "transport", None)
                if transport is not None:
                    await transport.aclose()
        updated = apply_probe(model, probed)
        self.repos.models.upsert(updated)
        return updated
