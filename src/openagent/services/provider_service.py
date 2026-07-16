"""Provider connection management (spec §12–§24, §30)."""

from __future__ import annotations

import contextlib
import hashlib
import os
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import TracebackType
from typing import TYPE_CHECKING
from uuid import uuid4

from ..core.models import (
    CredentialRef,
    CredentialType,
    ModelCapabilities,
    Protocol,
    ProviderConnection,
    RemoteModel,
    enum_value,
)
from ..credentials.redaction import redact, register_secret, secret_scope
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
            # A fresh revision per connection: the id is derived from the name, so re-adding a
            # removed provider reuses the id — without this, a persisted probe taken against the
            # *previous* key would still match the new connection's cache key (spec §22).
            credential_revision=uuid4().hex,
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
        # Purge this connection's probes before the row goes: the id is derived from the name, so a
        # re-add under the same name reuses it. The credential revision would reject the inherited
        # rows anyway; deleting them keeps the store from accumulating verdicts for a connection
        # that no longer exists (spec §22).
        self.repos.model_probes.delete_for_provider(provider.id)
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
        # Scoped, not registered forever (spec §8): this key belongs to a form the user is filling
        # in, not to a run. Every value leaving here is redacted *inside* the scope — releasing the
        # key first and redacting later (in the UI) would be too late for a prefixless key, which
        # only the exact-value registry can catch.
        with secret_scope(key):
            try:
                resolve_base_url(provider)
            except ValueError as exc:
                return HealthResult(ok=False, detail=redact(str(exc)))
            adapter = build_adapter(provider, key)
            try:
                return await adapter.test_connection()
            except Exception as exc:  # noqa: BLE001 - surface any failure as an unhealthy result
                return HealthResult(ok=False, detail=redact(str(exc)))
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
        # Scoped to this call (spec §8) — see test_config for why the key must not outlive the form.
        with secret_scope(key):
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
        if not refresh:
            cached = self._cached_probe(provider, model_id)
            if cached is not None:
                return cached
        adapter = self.adapter_for(provider)
        try:
            result = await probe_agent_model(adapter, model_id)
        finally:
            await _maybe_close(adapter)
        self._store_probe(provider, model_id, result)
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
        # Scoped to this call (spec §8) — see test_config for why the key must not outlive the form.
        with secret_scope(key):
            try:
                resolve_base_url(provider)
            except ValueError as exc:
                return AgentModelProbe(
                    model_id,
                    ModelCapabilities(text=False),
                    False,
                    PROBE_UNREACHABLE,
                    redact(str(exc)),
                )
            adapter = build_adapter(provider, key)
            try:
                probe = await probe_agent_model(adapter, model_id)
            finally:
                await _maybe_close(adapter)
            # `detail` carries whatever the provider said, which may quote the key back. Redact while
            # the scope still holds it — a prefixless key is unmatchable once released.
            return replace(probe, detail=redact(probe.detail))

    def cached_probe(self, provider_name: str, model_id: str) -> AgentModelProbe | None:
        """The stored probe for this model, or ``None`` — used to gate agent creation (§17.5, §22).

        Never triggers a probe: a caller that needs one must ask for it explicitly, so an expensive
        provider call is never made silently behind the user's back.
        """

        provider = self.get(provider_name)
        if not provider:
            return None
        return self._cached_probe(provider, model_id)

    def _cached_probe(self, provider: ProviderConnection, model_id: str) -> AgentModelProbe | None:
        """Read a persisted probe back, refusing anything that no longer describes reality (§22).

        The cache key already pins the connection, model, endpoint, protocol and credential
        revision, so a lookup miss *is* the invalidation for all of those: a changed provider
        simply computes a different key and finds nothing. Probe version and expiry are checked
        here as well, because a row can outlive both while its key still matches.
        """

        row = self.repos.model_probes.get(self._probe_key(provider, model_id))
        if row is None:
            return None
        cached = _probe_from_row(row)
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

    def _store_probe(
        self, provider: ProviderConnection, model_id: str, probe: AgentModelProbe
    ) -> None:
        """Persist a probe so the *next* process can honour it (spec §22).

        ``detail`` is redacted first: it carries whatever the provider said, and a provider that
        quotes the key back in an error body would otherwise write that key to the database.
        """

        stored = replace(probe, detail=redact(probe.detail), tested_at=probe.tested_at or _utcnow())
        tested_at = stored.tested_at
        assert tested_at is not None  # set on the line above
        self.repos.model_probes.put(
            cache_key=self._probe_key(provider, model_id),
            provider_id=provider.id,
            model_id=model_id,
            base_url_fingerprint=_fingerprint(self._base_url(provider)),
            protocol=enum_value(provider.protocol),
            credential_revision=self._credential_revision(provider),
            probe_version=stored.probe_version,
            tested_at=tested_at.isoformat(),
            data=stored.to_dict(),
        )

    def _probe_key(self, provider: ProviderConnection, model_id: str) -> str:
        """Everything the verdict depends on, hashed into one key (spec §22).

        Anything that changes what a probe would find must change this key, or a stale row answers
        for a connection it never saw. Note what is *absent*: no key, no digest of the key, nothing
        derived from the secret at all — §22 forbids persisting any of it, and this string is
        written to disk.
        """

        parts = "|".join(
            (
                provider.id,
                model_id,
                self._base_url(provider),
                enum_value(provider.protocol),
                self._credential_revision(provider),
                PROBE_VERSION,
            )
        )
        return _fingerprint(parts)

    def _base_url(self, provider: ProviderConnection) -> str:
        try:
            return resolve_base_url(provider)
        except ValueError:
            return ""

    def _credential_revision(self, provider: ProviderConnection) -> str:
        """Which credential this connection carries — a reference plus an opaque revision token.

        **Known limitation (spec §22, documented in SECURITY.md):** a key rotated *outside*
        OpenAgent — edited straight into the OS keychain, or an env var whose value changed —
        does not move the revision, so a probe taken with the previous key stays trusted until it
        expires (``PROBE_CACHE_TTL``). Detecting that would mean persisting something derived from
        the secret, which §22 forbids. Rotation *through* OpenAgent mints a new revision and
        invalidates immediately; the TTL bounds the rest.
        """

        credential = provider.credential
        ref = f"{enum_value(credential.type)}:{credential.account or credential.env_var or ''}"
        return f"{ref}:{provider.credential_revision or 'legacy'}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(value: str) -> str:
    """A short, stable digest of non-secret connection facts (endpoint, protocol, revision).

    Used for the probe cache key and the stored ``base_url_fingerprint``. Never applied to a
    secret — see ``_credential_revision`` for why the credential contributes a revision token
    rather than anything derived from the key itself.
    """

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _probe_from_row(row: dict) -> AgentModelProbe | None:
    """Rebuild a probe from its stored JSON, tolerating a row this build cannot read.

    A malformed or future-shaped row is treated as *no probe*, never as a verified one: the gate
    this feeds must fail closed (spec §22).
    """

    try:
        caps = row.get("capabilities")
        if isinstance(caps, dict):
            capabilities = ModelCapabilities(**caps)
        else:
            # `AgentModelProbe.to_dict` flattens the capabilities alongside the verdict.
            capabilities = ModelCapabilities(
                text=row.get("text"),
                streaming=row.get("streaming"),
                tool_calling=row.get("tool_calling"),
            )
        tested_at = row.get("tested_at")
        return AgentModelProbe(
            model=str(row["model"]),
            capabilities=capabilities,
            agent_compatible=bool(row.get("agent_compatible")),
            category=str(row.get("category", "")),
            detail=str(row.get("detail", "")),
            tested_at=datetime.fromisoformat(tested_at) if tested_at else None,
            probe_version=str(row.get("probe_version", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


async def _maybe_close(adapter: object) -> None:
    transport = getattr(adapter, "transport", None)
    if transport is not None and hasattr(transport, "aclose"):
        await transport.aclose()
