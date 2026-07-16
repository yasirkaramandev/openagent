"""Provider connection management (spec §12–§24, §30)."""

from __future__ import annotations

import contextlib
import hashlib
import os
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from types import TracebackType
from typing import TYPE_CHECKING

from ..core.models import (
    CredentialRef,
    CredentialType,
    ModelCapabilities,
    Protocol,
    ProviderConnection,
    RemoteModel,
    enum_value,
)
from ..credentials.redaction import register_secret
from ..providers.base import HealthResult
from ..providers.discovery import (
    PROBE_UNREACHABLE,
    PROBE_VERSION,
    AgentModelProbe,
    probe_agent_model,
)
from ..providers.factory import build_adapter, get_preset, resolve_base_url

if TYPE_CHECKING:
    from ..app import OpenAgentApp
    from ..credentials.store import CredentialStore
    from ..storage.repositories import Repositories

#: How long a capability probe stays trusted before it must be re-run (spec §16).
PROBE_CACHE_TTL = timedelta(hours=24)


class ProviderValidationError(ValueError):
    """A provider's credential configuration is invalid (missing key/env var, illegal 'none').

    Carries the offending form ``field`` so the TUI can surface the message inline under it.
    """

    def __init__(self, message: str, *, field: str = "") -> None:
        super().__init__(message)
        self.field = field


class ProviderInUseError(ValueError):
    """Raised when deleting a provider that one or more agents still bind to."""

    def __init__(self, provider: str, agents: Sequence[str]) -> None:
        self.provider = provider
        self.agents = list(agents)
        super().__init__(f"provider {provider!r} is used by agents: {', '.join(self.agents)}")


class ProviderTransaction:
    """A provider-creation transaction whose secret-rollback state lives ONLY on the stack (§6).

    The old design stashed a :class:`!SecretRollback` — including the previous keychain value in
    plaintext — in a service-level ``dict`` that was cleared only if ``rollback()`` was called. So a
    *successful* ``provider add`` left the user's old key sitting in ``ProviderService`` for the whole
    process lifetime. This transaction holds that value only for the duration of the ``with`` block:

    * ``__enter__`` writes the new secret (capturing the previous value to restore) and the provider
      row; a DB failure there restores the keychain and re-raises, and ``__exit__`` is not invoked.
    * ``commit()`` — called the moment the provider (and, in the connect-and-create-agent flow, the
      agent) is durably written — wipes the captured previous value immediately.
    * ``__exit__`` on an *uncommitted* transaction restores the keychain exactly as it was and deletes
      the half-written provider row.

    Nothing plaintext survives past the ``with`` block, committed or not.
    """

    def __init__(
        self,
        credentials: CredentialStore,
        repos: Repositories,
        provider: ProviderConnection,
        api_key: str | None,
    ) -> None:
        self._credentials = credentials
        self._repos = repos
        self.provider = provider
        self._api_key = api_key
        self._previous: str | None = None
        self._wrote = False
        self._committed = False

    def __enter__(self) -> ProviderTransaction:
        credential = self.provider.credential
        if self._api_key and credential.type is CredentialType.KEYCHAIN:
            # Capture the *value* that was there before (needed to restore it byte-for-byte), then
            # overwrite it. Held only until commit()/rollback.
            self._previous = self._credentials.resolve(credential)
            self._wrote = True
            self._credentials.set_secret(credential, self._api_key)
        try:
            self._repos.providers.upsert(self.provider)
        except Exception:
            self._restore()
            self._forget()
            raise
        self._api_key = None  # the key now lives in the keychain; keep no copy in memory
        return self

    def commit(self) -> None:
        """Mark the transaction durable and immediately forget the previous secret (§6.1)."""

        self._committed = True
        self._forget()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Returns None → never suppresses the in-flight exception; an uncommitted transaction is
        # rolled back exactly (keychain restored, half-written provider row removed).
        if not self._committed:
            self._restore()
            self._forget()
            with contextlib.suppress(Exception):
                self._repos.providers.delete(self.provider.id)

    def _restore(self) -> None:
        """Put the old secret back verbatim, or remove one that never existed (item 17)."""

        if not self._wrote or self.provider.credential.type is not CredentialType.KEYCHAIN:
            return
        if self._previous is None:
            self._credentials.delete_secret(self.provider.credential)
        else:
            self._credentials.set_secret(self.provider.credential, self._previous)

    def _forget(self) -> None:
        self._previous = None
        self._api_key = None
        self._wrote = False


def resolve_credential(
    *,
    name: str,
    provider_type: str,
    api_key: str | None,
    key_env: str | None,
    credential_source: str | None = None,
) -> CredentialRef:
    """Validate the credential inputs and build a :class:`CredentialRef`, or fail closed.

    This is the single source of truth for "is this provider's credential acceptable" — the CLI,
    the Add Provider screen, and the Add Agent connect-new flow all go through
    :meth:`ProviderService.add`, which calls this. Nothing is persisted if it raises.
    """

    preset = get_preset(provider_type)
    needs_key = preset.needs_key if preset else True
    # An explicit "no key" is only legitimate for providers that don't need one (ollama, LM Studio)
    # or a bespoke endpoint the user is knowingly configuring (custom / unknown preset).
    none_allowed = (not needs_key) or provider_type == "custom" or preset is None

    source = credential_source or ("env" if key_env else "keychain")
    key = (api_key or "").strip()
    env_var = (key_env or "").strip()

    if source == "env":
        if not env_var:
            raise ProviderValidationError(
                "environment variable name is required for an env-var credential", field="key_env"
            )
        return CredentialRef(type=CredentialType.ENV, env_var=env_var)

    if source == "none":
        if not none_allowed:
            raise ProviderValidationError(
                f"provider type {provider_type!r} requires a key; 'no key' is only for local "
                "providers (ollama, lmstudio) or a custom endpoint",
                field="api_key",
            )
        return CredentialRef(type=CredentialType.NONE)

    # Default: OS keychain.
    if not key:
        if needs_key:
            raise ProviderValidationError(
                "an API key is required for this provider", field="api_key"
            )
        # A no-key provider configured via the keychain source but left blank: store nothing.
        return CredentialRef(type=CredentialType.NONE)
    return CredentialRef(
        type=CredentialType.KEYCHAIN, service="openagent", account=f"provider/{name}"
    )


class ProviderService:
    def __init__(self, app: OpenAgentApp) -> None:
        self.app = app
        self.repos = app.repos
        self.credentials = app.credentials
        #: In-memory capability-probe cache (spec §16), keyed by connection id + model + base URL +
        #: a *credential identity* — never the secret itself, and never written to disk. The identity
        #: includes a short digest of the resolved key so that rotating the key invalidates a prior
        #: "verified" result instead of vouching for a key that was never tested.
        self._probe_cache: dict[str, AgentModelProbe] = {}

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
        credential_source: str | None = None,
        region: str | None = None,
        workspace_id: str | None = None,
        extra_headers: dict[str, str] | None = None,
        store_key: bool = True,
    ) -> ProviderConnection:
        """Register a provider and store its key in the OS keychain (spec §30).

        The credential is validated first (:func:`resolve_credential`) and this method **fails
        closed** and **atomically**:

        * a **duplicate name is rejected at the service layer** (item 6) — the CLI/TUI checks are a
          convenience, not the authority. ``upsert`` never silently overwrites an existing provider;
        * if the key/env-var is missing or 'no key' is illegal for this provider type, it raises
          :class:`ProviderValidationError` and writes nothing to the DB or keychain;
        * the keychain secret is written **before** the provider row, but if the row write fails the
          keychain is restored **exactly** as it was (item 17): a secret that existed before is put
          back verbatim, and one that did not is removed. A failed transaction can neither orphan a
          new key nor destroy an old one.

        ``key_env`` references an environment variable instead of storing a secret; ``api_key`` is
        written to the OS keychain. ``credential_source`` (``keychain``/``env``/``none``) makes the
        user's choice explicit; when omitted it is inferred from the inputs.
        """

        with self.create_transaction(
            name=name,
            provider_type=provider_type,
            protocol=protocol,
            base_url=base_url,
            anthropic_base_url=anthropic_base_url,
            api_key=api_key,
            key_env=key_env,
            credential_source=credential_source,
            region=region,
            workspace_id=workspace_id,
            extra_headers=extra_headers,
            store_key=store_key,
        ) as tx:
            tx.commit()  # the provider row is durable — forget the previous secret immediately (§6)
            return tx.provider

    def create_transaction(
        self,
        *,
        name: str,
        provider_type: str,
        protocol: Protocol | None = None,
        base_url: str | None = None,
        anthropic_base_url: str | None = None,
        api_key: str | None = None,
        key_env: str | None = None,
        credential_source: str | None = None,
        region: str | None = None,
        workspace_id: str | None = None,
        extra_headers: dict[str, str] | None = None,
        store_key: bool = True,
    ) -> ProviderTransaction:
        """Prepare a provider-creation transaction (validates + builds the row), for callers that need
        to commit only after a *partner* write succeeds — the atomic connect-provider-and-create-agent
        flow (item 3). ``__enter__`` writes the secret + row; the caller commits after its own write,
        and any exception before ``commit()`` rolls the keychain and row back exactly (§6.1)."""

        if self.get(name) is not None:
            raise ProviderValidationError(f"provider {name!r} already exists", field="name")
        preset = get_preset(provider_type)
        resolved_protocol = protocol or (preset.protocol if preset else Protocol.OPENAI_CHAT)
        credential = resolve_credential(
            name=name,
            provider_type=provider_type,
            api_key=api_key,
            key_env=key_env,
            credential_source=credential_source,
        )
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
        stored_key = api_key if (store_key and credential.type is CredentialType.KEYCHAIN) else None
        return ProviderTransaction(self.credentials, self.repos, provider, stored_key)

    def list(self) -> Sequence[ProviderConnection]:
        return self.repos.providers.list()

    def get(self, name: str) -> ProviderConnection | None:
        return self.repos.providers.get_by_name(name)

    def agents_using(self, name: str) -> Sequence[str]:
        """Names of agents whose runtime binds to the provider connection ``name``."""

        return [a.name for a in self.repos.agents.list() if a.runtime.provider == name]

    def remove(self, name: str) -> bool:
        """Delete a provider and its keychain secret.

        Refuses (raises :class:`ProviderInUseError`) when any agent still binds to it, so a
        dependent agent is never left pointing at a missing provider. Returns ``False`` when the
        provider does not exist.
        """

        provider = self.get(name)
        if not provider:
            return False
        dependents = self.agents_using(name)
        if dependents:
            raise ProviderInUseError(name, dependents)
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
            id="provider__transient",
            name="__transient",
            provider_type=provider_type,
            protocol=resolved_protocol,
            base_url=base_url,
            anthropic_base_url=anthropic_base_url,
            region=region,
            workspace_id=workspace_id,
            credential=CredentialRef(type=CredentialType.NONE),
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

    async def remote_models_config(
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
    ) -> Sequence[RemoteModel]:
        """List models for a *would-be* provider before it is saved (Add-Agent new-connection flow).

        Mirrors :meth:`test_config`: builds a transient adapter from the supplied fields, persists
        nothing, and never stores or echoes the key. Best-effort — returns ``[]`` on any failure.
        """

        preset = get_preset(provider_type)
        resolved_protocol = protocol or (preset.protocol if preset else Protocol.OPENAI_CHAT)
        provider = ProviderConnection(
            id="provider__transient",
            name="__transient",
            provider_type=provider_type,
            protocol=resolved_protocol,
            base_url=base_url,
            anthropic_base_url=anthropic_base_url,
            region=region,
            workspace_id=workspace_id,
            credential=CredentialRef(type=CredentialType.NONE),
        )
        key = api_key or (os.environ.get(key_env) if key_env else None)
        register_secret(key)
        try:
            resolve_base_url(provider)
        except ValueError:
            return []
        adapter = build_adapter(provider, key)
        try:
            return await adapter.list_models()
        except Exception:  # noqa: BLE001 - discovery is best-effort
            return []
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

    # ------------------------------------------------------------------ capability probe (§15, §16)

    async def probe_model(
        self,
        provider_name: str,
        model_id: str,
        *,
        refresh: bool = False,
    ) -> AgentModelProbe:
        """Really exercise ``model_id`` on a saved provider and report what was observed (§15.1).

        This — not ``/models`` reachability — is the only thing that may be called validation: a
        catalog can be public, so listing a model proves neither that the key works nor that the model
        speaks OpenAgent's chat/tool shape. Results are cached per (connection, model, base URL,
        credential identity) with a TTL (§16); ``refresh=True`` forces a fresh probe.
        """

        provider = self.get(provider_name)
        if not provider:
            raise ProviderValidationError(f"provider {provider_name!r} not found", field="name")
        key = self._probe_key(provider, model_id)
        if not refresh:
            cached = self._cached_probe(key)
            if cached is not None:
                return cached
        adapter = self.adapter_for(provider)
        try:
            result = await probe_agent_model(adapter, model_id)
        finally:
            await _maybe_close(adapter)
        self._probe_cache[key] = result
        return result

    async def probe_model_config(
        self,
        *,
        model_id: str,
        provider_type: str,
        protocol: Protocol | None = None,
        base_url: str | None = None,
        anthropic_base_url: str | None = None,
        region: str | None = None,
        workspace_id: str | None = None,
        api_key: str | None = None,
        key_env: str | None = None,
    ) -> AgentModelProbe:
        """Probe a model on a *would-be* provider before it is saved (Add-Agent new-connection flow).

        Mirrors :meth:`test_config`: a transient adapter, nothing persisted, the key never stored or
        echoed. Not cached — there is no connection identity to key a cache on yet.
        """

        preset = get_preset(provider_type)
        resolved_protocol = protocol or (preset.protocol if preset else Protocol.OPENAI_CHAT)
        provider = ProviderConnection(
            id="provider__transient",
            name="__transient",
            provider_type=provider_type,
            protocol=resolved_protocol,
            base_url=base_url,
            anthropic_base_url=anthropic_base_url,
            region=region,
            workspace_id=workspace_id,
            credential=CredentialRef(type=CredentialType.NONE),
        )
        key = api_key or (os.environ.get(key_env) if key_env else None)
        register_secret(key)
        try:
            resolve_base_url(provider)
        except ValueError as exc:
            return AgentModelProbe(
                model_id, ModelCapabilities(text=False), False, PROBE_UNREACHABLE, str(exc)
            )
        adapter = build_adapter(provider, key)
        try:
            return await probe_agent_model(adapter, model_id)
        finally:
            await _maybe_close(adapter)

    def cached_probe(self, provider_name: str, model_id: str) -> AgentModelProbe | None:
        """The cached probe for this model, or ``None`` — used to gate agent creation (§17.5).

        Never triggers a probe: a caller that needs one must ask for it explicitly, so an expensive
        provider call is never made silently behind the user's back.
        """

        provider = self.get(provider_name)
        if not provider:
            return None
        return self._cached_probe(self._probe_key(provider, model_id))

    def _cached_probe(self, key: str) -> AgentModelProbe | None:
        cached = self._probe_cache.get(key)
        if cached is None or cached.tested_at is None:
            return None
        if cached.probe_version != PROBE_VERSION:
            return None  # the probe definition changed — an old result proves nothing about the new one
        tested = cached.tested_at
        if tested.tzinfo is None:
            tested = tested.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - tested >= PROBE_CACHE_TTL:
            return None
        return cached

    def _probe_key(self, provider: ProviderConnection, model_id: str) -> str:
        try:
            base_url = resolve_base_url(provider)
        except ValueError:
            base_url = ""
        return (
            f"{provider.id}|{model_id}|{base_url}|{self._credential_identity(provider.credential)}"
        )

    def _credential_identity(self, credential: CredentialRef) -> str:
        """A stable identity for the credential — the *reference*, plus an in-memory-only digest.

        The digest exists so that rotating the key behind the same reference invalidates a cached
        "verified" result (§16). It is derived at call time and lives only in this process's cache
        dict; no secret or secret hash is ever written to disk.
        """

        ref = f"{enum_value(credential.type)}:{credential.account or credential.env_var or ''}"
        secret = self.credentials.resolve(credential)
        digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16] if secret else "nokey"
        return f"{ref}:{digest}"


async def _maybe_close(adapter: object) -> None:
    transport = getattr(adapter, "transport", None)
    if transport is not None and hasattr(transport, "aclose"):
        await transport.aclose()
