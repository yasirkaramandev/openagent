"""One adapter for every v0.2 provider (spec §6.1, §7, §9, §12–§21).

A provider adapter used to be a class per vendor. With protocol serializers in :mod:`.wire`, catalog
readers in :mod:`.model_catalog` and vendor differences in :mod:`.spec`, there is nothing vendor-shaped
left for a per-vendor class to hold — so there is one adapter, and a provider is a spec plus a chosen
protocol.

What this class adds on top of the wire is the part that is about *knowledge* rather than transport:

* a connection test that can tell "unreachable" from "rejected" from "reachable but the catalog is not
  listable", because those are three different next steps for the user;
* capability evidence with provenance — the catalog's claims recorded as
  :data:`EvidenceSource.PROVIDER_CATALOG`, a probe's findings as
  :data:`EvidenceSource.LIVE_PROBE`, and never a claim invented from a model's name;
* the refusal to send a credential over cleartext to anything but loopback.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ..core.errors import ErrorType
from ..core.events import NormalizedModelEvent
from ..core.models import ModelCapabilities, Protocol, RemoteModel
from .base import (
    HealthResult,
    NormalizedModelRequest,
    TokenEstimate,
    default_probe,
    rough_token_estimate,
)
from .compat.evidence import (
    Capability,
    CapabilityEvidence,
    CapabilityLedger,
    CapabilityStatus,
    EvidenceSource,
)
from .compat.profiles_v2 import AuthScheme, CompatibilityProfile, profile_for
from .continuation import ContinuationEnvelope, ContinuationStrategy
from .model_catalog import CatalogResult, discover_catalog
from .spec import ProviderSpec, get_spec, is_loopback, requires_tls, spec_names
from .transport import Transport, TransportError
from .wire.anthropic_messages import AnthropicMessagesWire
from .wire.lmstudio_native import LmStudioNativeWire
from .wire.ollama_native import OllamaNativeWire
from .wire.openai_chat import OpenAIChatWire
from .wire.openai_responses import OpenAIResponsesWire

#: Bumped when :func:`probe_evidence`'s request shapes or success rules change, so a cached verdict
#: from an older definition is treated as stale rather than inherited (spec §9).
EVIDENCE_PROBE_VERSION = 1


class InsecureEndpointError(ValueError):
    """A credential would have crossed the network in cleartext."""


@dataclass(frozen=True)
class ProbeOutcome:
    """What a live probe established, and what it could not."""

    ledger: CapabilityLedger
    #: Set when the probe could not run at all (unreachable, rejected). The ledger is then empty
    #: rather than full of ``UNSUPPORTED`` — a probe that never ran has disproved nothing.
    blocked_by: ErrorType | None = None
    detail: str = ""

    @property
    def ran(self) -> bool:
        return self.blocked_by is None


class WireProviderAdapter:
    """The :class:`~.base.ProviderAdapter` contract, over any protocol, for any v0.2 provider."""

    def __init__(
        self,
        *,
        spec: ProviderSpec,
        api_key: str | None = None,
        region: str | None = None,
        protocol: Protocol | None = None,
        base_url: str | None = None,
        workspace_id: str | None = None,
        extra_headers: dict[str, str] | None = None,
        reasoning_effort: str | None = None,
        thinking_budget: int | None = None,
        store: bool = False,
        previous_response_id: str | None = None,
        keep_alive: str | None = None,
        transport: Transport | None = None,
        allow_insecure_http: bool = False,
    ) -> None:
        self.spec = spec
        self.region_id = region or spec.default_region
        resolved_protocol, resolved_url = spec.resolve(region_id=self.region_id, protocol=protocol)
        self.protocol = resolved_protocol
        # An explicit base_url overrides the region's default (a self-hosted endpoint, a gateway) but
        # does not change the region: the region still says which credential population this is.
        self.base_url = (base_url or resolved_url).rstrip("/")
        self.workspace_id = workspace_id
        self.profile: CompatibilityProfile = profile_for(spec.provider_type, self.protocol)

        if requires_tls(self.base_url, local=spec.local) and not allow_insecure_http:
            raise InsecureEndpointError(
                f"{spec.label} would be reached over plain HTTP at a non-loopback address; a "
                f"credential sent there crosses the network in cleartext. Use https, or set "
                f"allow_insecure_http for a deliberately trusted network."
            )

        headers = self._headers(api_key, extra_headers)
        if transport is None:
            self.transport = Transport(base_url=self.base_url, headers=headers)
        else:
            # An injected transport controls the base URL, timeouts and retry policy — that is what
            # callers inject one for. It must not also silently drop the credential: a transport
            # passed in without auth headers would send unauthenticated requests and report the
            # provider's 401 as though the key were wrong. Auth is merged in, and anything the
            # caller set explicitly wins.
            self.transport = transport
            for key, value in headers.items():
                self.transport.headers.setdefault(key, value)
        self.wire = self._build_wire(
            reasoning_effort=reasoning_effort,
            thinking_budget=thinking_budget,
            store=store,
            previous_response_id=previous_response_id,
            keep_alive=keep_alive,
        )
        self._last_catalog: CatalogResult | None = None

    # ------------------------------------------------------------------ construction

    def _headers(self, api_key: str | None, extra: dict[str, str] | None) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        scheme = self.profile.auth_scheme
        if api_key:
            if scheme is AuthScheme.API_KEY_HEADER or self.protocol is Protocol.ANTHROPIC_MESSAGES:
                # The Messages API authenticates with x-api-key and requires a version header,
                # whoever is serving it.
                headers["x-api-key"] = api_key
                headers.setdefault("anthropic-version", "2023-06-01")
            elif scheme is AuthScheme.GOOGLE_API_KEY:
                # A header, not the `?key=` query parameter: a key in a URL lands in proxy logs and
                # anything that records a request line.
                headers["x-goog-api-key"] = api_key
            else:
                # Reached for AuthScheme.NONE too, and deliberately. NONE means the provider does not
                # *require* a credential, not that it cannot accept one: a remote Ollama or LM Studio
                # behind a reverse proxy takes a bearer token (spec §12.3, §13.4). Dropping a key the
                # caller supplied would send unauthenticated requests and surface the proxy's 401 as
                # though the key were wrong.
                headers["Authorization"] = f"Bearer {api_key}"
        if self.workspace_id:
            # Qwen scopes a key to a workspace with a header rather than a path segment.
            headers["X-DashScope-WorkSpace"] = self.workspace_id
        if extra:
            headers.update(extra)
        return headers

    def _build_wire(self, **kwargs: Any) -> Any:
        reasoning_effort = kwargs.get("reasoning_effort")
        if self.protocol is Protocol.ANTHROPIC_MESSAGES:
            return AnthropicMessagesWire(
                profile=self.profile,
                transport=self.transport,
                reasoning_effort=reasoning_effort,
                thinking_budget=kwargs.get("thinking_budget"),
            )
        if self.protocol is Protocol.OPENAI_RESPONSES:
            return OpenAIResponsesWire(
                profile=self.profile,
                transport=self.transport,
                reasoning_effort=reasoning_effort,
                store=bool(kwargs.get("store")),
                previous_response_id=kwargs.get("previous_response_id"),
            )
        if self.protocol is Protocol.OLLAMA_NATIVE_CHAT:
            return OllamaNativeWire(
                profile=self.profile,
                transport=self.transport,
                reasoning_effort=reasoning_effort,
                keep_alive=kwargs.get("keep_alive"),
            )
        if self.protocol is Protocol.LMSTUDIO_NATIVE_CHAT:
            return LmStudioNativeWire(
                profile=self.profile,
                transport=self.transport,
                reasoning_effort=reasoning_effort,
            )
        if self.protocol is Protocol.GEMINI_INTERACTIONS:
            from .gemini_interactions import GeminiInteractionsAdapter

            return GeminiInteractionsAdapter(
                api_key=None,
                base_url=self.base_url,
                store=bool(kwargs.get("store")),
                previous_interaction_id=kwargs.get("previous_response_id"),
                transport=self.transport,
            )
        return OpenAIChatWire(
            profile=self.profile,
            transport=self.transport,
            reasoning_effort=reasoning_effort,
            thinking_budget=kwargs.get("thinking_budget"),
        )

    # ------------------------------------------------------------------ health

    async def test_connection(self) -> HealthResult:
        """Distinguish unreachable, rejected, and reachable-but-not-listable.

        A provider whose catalog is not listable is *healthy*; treating that as a failed connection
        would make every manual-only provider look broken. Only a rejected credential or an
        unreachable endpoint is a failure.
        """

        catalog = await self.catalog()
        if catalog.ok or catalog.partial:
            detail = "reachable" if not catalog.partial else f"reachable; {catalog.summary()}"
            return HealthResult(ok=True, detail=detail)
        if catalog.manual_only:
            return HealthResult(ok=True, detail="reachable; no listable catalog")
        if catalog.error_type in {"unauthorized", "forbidden"}:
            return HealthResult(ok=False, detail=f"credential rejected ({catalog.error_type})")
        if catalog.error_type == "endpoint_unsupported":
            # The endpoint answered something; it just does not serve a model list. Not a failure.
            return HealthResult(ok=True, detail="reachable; endpoint does not list models")
        return HealthResult(ok=False, detail=catalog.summary())

    # ------------------------------------------------------------------ discovery

    async def catalog(self, *, refresh: bool = False) -> CatalogResult:
        if self._last_catalog is None or refresh:
            self._last_catalog = await discover_catalog(
                self.spec.discovery, self.transport, provider_type=self.spec.provider_type
            )
        return self._last_catalog

    async def list_models(self) -> list[RemoteModel]:
        """The :class:`ProviderAdapter` contract's flat list.

        Callers that need to distinguish empty from unreadable use :meth:`catalog` instead; this
        raises on an unreadable catalog so the protocol's own contract ("a list, or an error") holds.
        """

        catalog = await self.catalog()
        if not catalog.ok and not catalog.entries and not catalog.manual_only:
            raise TransportError(
                ErrorType.CATALOG_UNAVAILABLE,
                catalog.error_message or "the provider's model catalog could not be read",
            )
        return catalog.models

    # ------------------------------------------------------------------ capabilities

    async def probe_model(self, model_id: str) -> ModelCapabilities:
        return await default_probe(self, model_id)

    async def probe_evidence(self, model_id: str, *, timeout: float = 30.0) -> ProbeOutcome:
        """Establish capabilities by observation, and record how each one was established.

        The rules that make this worth having rather than a bag of booleans:

        * a capability is ``SUPPORTED`` only when the request shape that proves it actually produced
          the thing;
        * one failed request does not mark every capability ``UNSUPPORTED`` — a rejected credential
          means the probe *did not run*, which is reported as ``blocked_by`` with an empty ledger;
        * a capability the probe did not exercise is simply absent, not ``UNSUPPORTED``.
        """

        from .base import Message, Role, collect

        catalog = await self.catalog()
        entry = next((e for e in catalog.entries if e.model.id == model_id), None)
        revision = entry.revision if entry else None
        # The catalog's claims are the floor. A probe result outranks them; a probe that could not
        # run leaves them standing, which is the whole point of a ranked ledger.
        ledger = entry.ledger() if entry else CapabilityLedger()

        def record(capability: Capability, status: CapabilityStatus, detail: str = "") -> None:
            nonlocal ledger
            ledger = ledger.record(
                CapabilityEvidence(
                    capability=capability,
                    status=status,
                    source=EvidenceSource.LIVE_PROBE,
                    probe_version=EVIDENCE_PROBE_VERSION,
                    model_revision=revision,
                    detail=detail,
                )
            )

        sentinel = "PROBE_OK_7F"
        text_request = NormalizedModelRequest(
            model=model_id,
            system=f"You are a probe. Reply with exactly this token and nothing else: {sentinel}",
            messages=[Message(role=Role.USER, content="Follow your instructions.")],
            max_tokens=16,
            stream=False,
        )
        try:
            result = await asyncio.wait_for(collect(self.stream_response(text_request)), timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return ProbeOutcome(ledger, ErrorType.TIMEOUT, "the model did not answer in time")
        except Exception as exc:  # noqa: BLE001 - a probe failure asserts nothing
            return ProbeOutcome(ledger, ErrorType.UNKNOWN, str(exc)[:200])
        if result.is_error:
            blocked = _blocking_error(result.error_type)
            if blocked is not None:
                # Unauthorized / not found / rate limited: nothing was learned about the model.
                return ProbeOutcome(ledger, blocked, result.error_message or "")
            record(Capability.TEXT, CapabilityStatus.UNSUPPORTED, result.error_message or "")
            return ProbeOutcome(ledger)

        record(
            Capability.TEXT,
            CapabilityStatus.SUPPORTED if result.text else CapabilityStatus.UNSUPPORTED,
        )
        if sentinel in (result.text or ""):
            record(Capability.SYSTEM_PROMPT, CapabilityStatus.SUPPORTED, "the sentinel was echoed")

        try:
            streamed = await asyncio.wait_for(
                collect(self.stream_response(text_request.model_copy(update={"stream": True}))),
                timeout,
            )
            if not streamed.is_error and streamed.text:
                record(Capability.STREAMING, CapabilityStatus.SUPPORTED)
        except Exception:  # noqa: BLE001 - leave streaming unrecorded, never UNSUPPORTED
            pass

        tool_request = NormalizedModelRequest(
            model=model_id,
            messages=[
                Message(role=Role.USER, content="Call ping with value 1, then pong with value 2.")
            ],
            tools=[
                {
                    "name": "ping",
                    "description": "probe",
                    "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}},
                },
                {
                    "name": "pong",
                    "description": "probe",
                    "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}},
                },
            ],
            max_tokens=128,
            stream=False,
        )
        try:
            tooled = await asyncio.wait_for(collect(self.stream_response(tool_request)), timeout)
            if not tooled.is_error and tooled.tool_calls:
                record(Capability.TOOL_CALLING, CapabilityStatus.SUPPORTED)
                if len(tooled.tool_calls) > 1:
                    # Two calls in one turn is the only thing that proves parallel calling. One call
                    # proves nothing either way, so nothing is recorded for it.
                    record(Capability.PARALLEL_TOOL_CALLING, CapabilityStatus.SUPPORTED)
        except Exception:  # noqa: BLE001 - absence does not disprove support
            pass

        if self.spec.supports_server_state:
            record(
                Capability.SERVER_SIDE_SESSION,
                CapabilityStatus.SUPPORTED,
                "the provider offers server-side state (opt-in)",
            )
        record(Capability.CLIENT_HISTORY_RESUME, CapabilityStatus.SUPPORTED, "history replay")
        return ProbeOutcome(ledger)

    # ------------------------------------------------------------------ execution

    def stream_response(
        self, request: NormalizedModelRequest
    ) -> AsyncIterator[NormalizedModelEvent]:
        return (
            self.wire.events(request)
            if hasattr(self.wire, "events")
            else self.wire.stream_response(request)
        )

    async def count_tokens(self, request: NormalizedModelRequest) -> TokenEstimate:
        """Ask the provider when it offers an endpoint; estimate locally otherwise, and say so.

        A local estimate presented as a provider count is a number people budget against. When the
        provider has no endpoint the estimate is still returned — it is useful — but nothing here
        claims it is authoritative.
        """

        if self.spec.token_count_path:
            try:
                payload = self._token_count_payload(request)
                data = await self.transport.post_json(self.spec.token_count_path, payload)
                counted = _extract_token_count(data)
                if counted is not None:
                    return TokenEstimate(input_tokens=counted)
            except (TransportError, ValueError):
                # Falling through to the estimate is right: a failed count must not fail the turn.
                pass
        return rough_token_estimate(request)

    def _token_count_payload(self, request: NormalizedModelRequest) -> dict[str, Any]:
        payload, _ = self.wire.build_payload(request, stream=False)
        return {"model": request.model, "messages": payload.get("messages", [])}

    # ------------------------------------------------------------------ continuation

    def build_continuation(self, *, model_id: str | None = None) -> ContinuationEnvelope:
        builder = getattr(self.wire, "build_continuation", None)
        if builder is None:  # pragma: no cover - every wire implements it
            return ContinuationEnvelope.build(
                provider_type=self.spec.provider_type,
                protocol=self.protocol,
                strategy=ContinuationStrategy.NORMALIZED_HISTORY,
                model_id=model_id,
            )
        return builder(model_id=model_id)

    # ------------------------------------------------------------------ reporting

    def describe(self) -> dict[str, Any]:
        """A secret-free summary for Doctor and the wizard."""

        return {
            "provider_type": self.spec.provider_type,
            "label": self.spec.label,
            "protocol": self.protocol.value,
            "region": self.region_id,
            "base_url": self.base_url,
            "tls": self.base_url.startswith("https://"),
            "loopback": is_loopback(self.base_url),
            "local": self.spec.local,
            "workspace_id": self.workspace_id,
            "discovery": self.spec.discovery.value,
            "supports_server_state": self.spec.supports_server_state,
            "auth_scheme": self.profile.auth_scheme.value,
            "error_mapper": self.profile.error_mapper,
        }

    async def aclose(self) -> None:
        await self.transport.aclose()


#: Errors that mean the probe never got to observe the model. Recording ``UNSUPPORTED`` for these
#: would let a rate limit or a typo'd key permanently mark a working model as incapable.
_BLOCKING = {
    ErrorType.AUTHENTICATION_FAILED,
    ErrorType.PERMISSION_DENIED,
    ErrorType.PROVIDER_REGION_MISMATCH,
    ErrorType.PROVIDER_RATE_LIMITED,
    ErrorType.PROVIDER_OVERLOADED,
    ErrorType.MODEL_NOT_FOUND,
    ErrorType.INSUFFICIENT_BALANCE,
    ErrorType.LOCAL_SERVER_UNAVAILABLE,
    ErrorType.LOCAL_MODEL_NOT_LOADED,
    ErrorType.LOCAL_MODEL_OUT_OF_MEMORY,
    ErrorType.NETWORK_UNAVAILABLE,
    ErrorType.TLS_ERROR,
    ErrorType.TIMEOUT,
}


def _blocking_error(error_type: str | None) -> ErrorType | None:
    if not error_type:
        return None
    try:
        parsed = ErrorType(error_type)
    except ValueError:
        return None
    return parsed if parsed in _BLOCKING else None


def _extract_token_count(data: dict[str, Any]) -> int | None:
    """Pull a token count out of whichever field a provider used, without inventing one."""

    for key in ("total_tokens", "input_tokens", "prompt_tokens", "token_count"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    for key in ("total_tokens", "input_tokens", "prompt_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    data_list = data.get("data")
    if isinstance(data_list, list) and data_list and isinstance(data_list[0], dict):
        value = data_list[0].get("total_tokens")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def build_provider_adapter(provider_type: str, **kwargs: Any) -> WireProviderAdapter:
    """Construct an adapter by provider name."""

    spec = get_spec(provider_type)
    if spec is None:
        raise KeyError(f"unknown v0.2 provider {provider_type!r}; known: {spec_names()}")
    return WireProviderAdapter(spec=spec, **kwargs)
