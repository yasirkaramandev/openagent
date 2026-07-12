"""Provider connection management (spec §12–§24, §30)."""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..core.models import CredentialRef, CredentialType, Protocol, ProviderConnection, RemoteModel
from ..credentials.redaction import register_secret
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
        key_env: str | None = None,
        region: str | None = None,
        workspace_id: str | None = None,
        extra_headers: dict[str, str] | None = None,
        store_key: bool = True,
    ) -> ProviderConnection:
        """Register a provider and store its key in the OS keychain (spec §30).

        ``key_env`` references an environment variable instead of storing a secret (nothing is
        persisted). Otherwise ``api_key`` is written to the OS keychain.
        """

        preset = get_preset(provider_type)
        resolved_protocol = protocol or (preset.protocol if preset else Protocol.OPENAI_CHAT)
        if key_env:
            credential = CredentialRef(type=CredentialType.ENV, env_var=key_env)
        else:
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

    def list(self) -> Sequence[ProviderConnection]:
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
        # Register the concrete key so it is scrubbed from every artifact/log even when its format
        # has no recognizable prefix (spec §30).
        register_secret(api_key)
        return build_adapter(provider, api_key)

    async def test_config(
        self,
        *,
        provider_type: str,
        protocol: Protocol | None = None,
        base_url: str | None = None,
        anthropic_base_url: str | None = None,
        region: str | None = None,
        workspace_id: str | None = None,
        api_key: str | None = None,
        key_env: str | None = None,
    ) -> HealthResult:
        """Test a would-be provider *before* saving it (spec §31 Test Connection).

        Builds a transient adapter from the supplied fields and key — nothing is persisted and the
        key is never stored or echoed back.
        """

        preset = get_preset(provider_type)
        resolved_protocol = protocol or (preset.protocol if preset else Protocol.OPENAI_CHAT)
        provider = ProviderConnection(
            id="provider__transient", name="__transient", provider_type=provider_type,
            protocol=resolved_protocol, base_url=base_url, anthropic_base_url=anthropic_base_url,
            region=region, workspace_id=workspace_id, credential=CredentialRef(type=CredentialType.NONE),
        )
        key = api_key or (os.environ.get(key_env) if key_env else None)
        register_secret(key)
        try:
            resolve_base_url(provider)
        except ValueError as exc:
            return HealthResult(ok=False, detail=str(exc))
        adapter = build_adapter(provider, key)
        try:
            return await adapter.test_connection()
        except Exception as exc:  # noqa: BLE001 - surface any failure as an unhealthy result
            return HealthResult(ok=False, detail=str(exc))
        finally:
            await _maybe_close(adapter)

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

    async def remote_models(self, name: str) -> Sequence[RemoteModel]:
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
