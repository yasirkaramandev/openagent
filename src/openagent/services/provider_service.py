"""Provider connection management (spec §12–§24, §30)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.models import CredentialRef, CredentialType, Protocol, ProviderConnection, RemoteModel
from ..providers.base import HealthResult
from ..providers.factory import build_adapter, get_preset, resolve_base_url

if TYPE_CHECKING:
    from ..app import OpenAgentApp


class ProviderService:
    def __init__(self, app: OpenAgentApp) -> None:
        self.app = app
        self.repos = app.repos
        self.credentials = app.credentials

    def add(
        self,
        *,
        name: str,
        provider_type: str,
        protocol: Protocol | None = None,
        base_url: str | None = None,
        anthropic_base_url: str | None = None,
        api_key: str | None = None,
        region: str | None = None,
        workspace_id: str | None = None,
        extra_headers: dict[str, str] | None = None,
        store_key: bool = True,
    ) -> ProviderConnection:
        """Register a provider and store its key in the OS keychain (spec §30)."""

        preset = get_preset(provider_type)
        resolved_protocol = protocol or (preset.protocol if preset else Protocol.OPENAI_CHAT)
        credential = CredentialRef(type=CredentialType.KEYCHAIN, service="openagent",
                                   account=f"provider/{name}")
        if not api_key and preset and not preset.needs_key:
            credential = CredentialRef(type=CredentialType.NONE)

        provider = ProviderConnection(
            id=f"provider_{name}",
            name=name,
            provider_type=provider_type,
            protocol=resolved_protocol,
            base_url=base_url,
            anthropic_base_url=anthropic_base_url,
            credential=credential,
            region=region,
            workspace_id=workspace_id,
            extra_headers=extra_headers or {},
        )
        if api_key and store_key and credential.type is CredentialType.KEYCHAIN:
            self.credentials.set_secret(credential, api_key)
        self.repos.providers.upsert(provider)
        return provider

    def list(self) -> list[ProviderConnection]:
        return self.repos.providers.list()

    def get(self, name: str) -> ProviderConnection | None:
        return self.repos.providers.get_by_name(name)

    def remove(self, name: str) -> bool:
        provider = self.get(name)
        if not provider:
            return False
        if provider.credential.type is CredentialType.KEYCHAIN:
            self.credentials.delete_secret(provider.credential)
        self.repos.providers.delete(provider.id)
        return True

    def adapter_for(self, provider: ProviderConnection):
        api_key = self.credentials.resolve(provider.credential)
        return build_adapter(provider, api_key)

    async def test(self, name: str) -> HealthResult:
        provider = self.get(name)
        if not provider:
            return HealthResult(ok=False, detail="provider not found")
        try:
            resolve_base_url(provider)
        except ValueError as exc:
            return HealthResult(ok=False, detail=str(exc))
        adapter = self.adapter_for(provider)
        try:
            return await adapter.test_connection()
        finally:
            await _maybe_close(adapter)

    async def remote_models(self, name: str) -> list[RemoteModel]:
        provider = self.get(name)
        if not provider:
            return []
        adapter = self.adapter_for(provider)
        try:
            return await adapter.list_models()
        except Exception:  # noqa: BLE001 - discovery is best-effort
            return []
        finally:
            await _maybe_close(adapter)


async def _maybe_close(adapter: object) -> None:
    transport = getattr(adapter, "transport", None)
    if transport is not None and hasattr(transport, "aclose"):
        await transport.aclose()
