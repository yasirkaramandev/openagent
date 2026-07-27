"""One catalog reader per discovery strategy (spec §7, §11.1, §12.1, §13.1, §14, §16.4).

Nine providers enumerate models nine different ways, and the *shapes* of the answers differ enough
that the readers cannot be merged. What can and must be merged is how the four outcomes are reported,
because three of them get collapsed into "no models found" by anyone writing this per provider:

* a **valid empty** catalog — the provider genuinely offers nothing;
* a **partial** catalog — some entries parsed, some did not; the usable ones are still offered and the
  gap is stated;
* an **unreadable** catalog — classified, so the wizard can offer retry / cached / manual instead of
  presenting an empty list as authoritative;
* **manual-only** — a first-class configuration for a provider with no listable catalog, not an error.

The other thing centralized here is the distinction v0.2 rests on: a catalog *claim* is
:class:`~.compat.evidence.CapabilityEvidence` sourced ``PROVIDER_CATALOG``, never a fact. A hosted
catalog can advertise a capability the deployment does not have, so a live probe outranks it. Model
names are never read for capability hints — a model called ``qwen3-tools`` does not thereby support
tools (spec §12.5).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import ErrorType
from ..core.models import DiscoveryStrategy, RemoteModel
from ..credentials.redaction import redact
from .base import ModelCatalogError, parse_model_catalog
from .compat.evidence import (
    Capability,
    CapabilityEvidence,
    CapabilityLedger,
    CapabilityStatus,
    EvidenceSource,
)
from .transport import Transport, TransportError

#: Bumped whenever a curated manifest entry changes. A manifest is the weakest evidence source and
#: ages the moment a provider ships a new revision, so its version is recorded with every claim it
#: produces — otherwise there is no way to tell a stale preset from a current one.
CURATED_MANIFEST_VERSION = "2026-07-27"

#: How many models an Ollama ``/api/show`` fan-out will describe. A machine with hundreds of pulled
#: models should not turn catalog discovery into hundreds of sequential local requests.
_SHOW_LIMIT = 64
#: Concurrency for that fan-out. Bounded because these are local requests against a single-process
#: daemon; flooding it makes discovery slower, not faster.
_SHOW_CONCURRENCY = 8


@dataclass
class CatalogEntry:
    """One model as a catalog described it, plus what that description is evidence *of*."""

    model: RemoteModel
    #: Capability claims made by the catalog itself. Empty when the catalog said nothing — which is
    #: different from saying no, and is why the values are statuses rather than booleans.
    claims: dict[Capability, CapabilityStatus] = field(default_factory=dict)
    #: Provider-announced removal. Kept visible rather than filtered out: a user already running the
    #: model needs to see it and be told to migrate (spec §15.1).
    deprecated: bool = False
    #: A stable identity for *this* revision of the model — a digest, a snapshot date, an
    #: architecture/quantization pair. Capability evidence is bound to it, because a capability
    #: verified against one revision says nothing about the next.
    revision: str | None = None
    #: Local providers only: whether the model is resident in memory right now.
    loaded: bool | None = None
    #: How strong this entry's claims are. Lives on the entry rather than being decided by whoever
    #: builds the ledger: a curated manifest recorded at PROVIDER_CATALOG strength is a preset
    #: wearing the clothes of a finding, and it would then outrank a real catalog and lose only to a
    #: probe. Making it a field means the one place that knows also decides.
    source: EvidenceSource = EvidenceSource.PROVIDER_CATALOG
    #: Short provenance note carried into every claim, e.g. the manifest version.
    claim_detail: str = "reported by the provider's model catalog"
    #: ``False`` for catalog entries that are demonstrably not chat models (an embeddings model).
    #: ``True`` is never asserted from a listing — that requires a probe (spec §14.3).
    chat_capable: bool | None = None
    prompt_price: float | None = None
    completion_price: float | None = None

    def ledger(self) -> CapabilityLedger:
        """The catalog's claims, as evidence a live probe can outrank."""

        ledger = CapabilityLedger()
        for capability, status in self.claims.items():
            ledger = ledger.record(
                CapabilityEvidence(
                    capability=capability,
                    status=status,
                    source=self.source,
                    model_revision=self.revision,
                    detail=self.claim_detail,
                )
            )
        return ledger


@dataclass
class CatalogResult:
    """The outcome of one discovery attempt, with every state distinguishable."""

    source: str
    ok: bool
    entries: list[CatalogEntry] = field(default_factory=list)
    partial: bool = False
    #: This provider has no listable catalog. A supported configuration: the user types a model id.
    manual_only: bool = False
    error_type: str | None = None
    error_message: str | None = None
    #: Set only for :data:`DiscoveryStrategy.CURATED_CATALOG`.
    manifest_version: str | None = None

    @property
    def models(self) -> list[RemoteModel]:
        return [entry.model for entry in self.entries]

    def summary(self) -> str:
        """A single honest line for Doctor and the wizard."""

        if self.manual_only:
            return (
                "no listable catalog; enter a model id manually or use the provider's default"
                if self.ok
                else f"manual model entry only ({self.error_message})"
            )
        if not self.ok and not self.entries:
            return f"catalog unavailable ({self.error_type}): {self.error_message}"
        if self.partial:
            return f"{len(self.entries)} model(s) read; some entries were unreadable"
        return f"{len(self.entries)} model(s)"

    def as_error_type(self) -> ErrorType | None:
        if self.ok and not self.partial:
            return None
        if self.entries:
            return ErrorType.CATALOG_PARTIAL
        return ErrorType.CATALOG_UNAVAILABLE


# --------------------------------------------------------------------------- entry point


async def discover_catalog(
    strategy: DiscoveryStrategy,
    transport: Transport,
    *,
    provider_type: str = "",
) -> CatalogResult:
    """Read a provider's catalog using ``strategy``. Never raises; always classifies."""

    if strategy is DiscoveryStrategy.MANUAL_ONLY:
        # No request is made. A provider declaring itself manual-only has not failed at anything.
        return CatalogResult(source="manual-only", ok=True, manual_only=True)

    reader = {
        DiscoveryStrategy.OPENAI_MODELS: _openai_models,
        DiscoveryStrategy.OPENROUTER_CATALOG: _openrouter_catalog,
        DiscoveryStrategy.OLLAMA_TAGS_SHOW: _ollama_tags_show,
        DiscoveryStrategy.LMSTUDIO_NATIVE: _lmstudio_native,
        DiscoveryStrategy.GEMINI_MODELS: _gemini_models,
    }.get(strategy)

    if reader is None:
        if strategy is DiscoveryStrategy.CURATED_CATALOG:
            return _curated(provider_type)
        return CatalogResult(
            source=strategy.value,
            ok=False,
            manual_only=True,
            error_type="endpoint_unsupported",
            error_message=f"no catalog reader for {strategy.value}",
        )

    try:
        return await reader(transport)
    except TransportError as exc:
        return _failed(strategy.value, exc)
    except ModelCatalogError as exc:
        return CatalogResult(
            source=strategy.value,
            ok=False,
            entries=[CatalogEntry(model=model) for model in exc.models],
            partial=bool(exc.models),
            error_type="malformed_response",
            error_message=redact(str(exc))[:512],
        )
    except Exception as exc:  # noqa: BLE001 - classified result; cancellation stays BaseException
        return CatalogResult(
            source=strategy.value,
            ok=False,
            error_type="malformed_response",
            error_message=redact(str(exc))[:512],
        )


def _failed(source: str, exc: TransportError) -> CatalogResult:
    error_type = {
        ErrorType.AUTHENTICATION_FAILED: "unauthorized",
        ErrorType.PERMISSION_DENIED: "forbidden",
        ErrorType.PROVIDER_RATE_LIMITED: "rate_limited",
        ErrorType.TIMEOUT: "timeout",
        ErrorType.NETWORK_UNAVAILABLE: "network",
        ErrorType.TLS_ERROR: "tls",
        ErrorType.CONNECTION_LOST: "network",
        ErrorType.LOCAL_SERVER_UNAVAILABLE: "local_server_unavailable",
        ErrorType.MODEL_NOT_FOUND: "endpoint_unsupported",
    }.get(exc.error_type, "endpoint_unsupported")
    return CatalogResult(
        source=source,
        ok=False,
        error_type=error_type,
        # Already redacted at TransportError construction; redacted again because this string becomes
        # durable in a Doctor report and the cost of the second pass is nothing.
        error_message=redact(exc.message)[:512] or error_type.replace("_", " "),
    )


# --------------------------------------------------------------------------- OpenAI /models


async def _openai_models(transport: Transport) -> CatalogResult:
    data = await transport.get_json("/models")
    try:
        models = parse_model_catalog(data)
    except ModelCatalogError as exc:
        return CatalogResult(
            source="openai-models",
            ok=False,
            entries=[CatalogEntry(model=model) for model in exc.models],
            partial=bool(exc.models),
            error_type="malformed_response",
            error_message=redact(str(exc))[:512],
        )
    return CatalogResult(
        source="openai-models",
        ok=True,
        entries=[
            CatalogEntry(model=model, revision=_created_revision(data, model.id))
            for model in models
        ],
    )


def _created_revision(payload: dict[str, Any], model_id: str) -> str | None:
    """Use the catalog's ``created`` stamp as a revision handle when it offers one."""

    for item in payload.get("data") or []:
        if isinstance(item, dict) and item.get("id") == model_id:
            created = item.get("created")
            if isinstance(created, int) and created > 0:
                return str(created)
    return None


# --------------------------------------------------------------------------- OpenRouter


#: OpenRouter's ``supported_parameters`` is documented as the full set a model accepts, so a missing
#: entry is a real negative rather than silence. That makes it the one catalog here allowed to record
#: UNSUPPORTED — everywhere else absence means "the catalog did not say".
_OPENROUTER_PARAMETER_CAPABILITIES = {
    "tools": Capability.TOOL_CALLING,
    "tool_choice": Capability.TOOL_CALLING,
    "reasoning": Capability.REASONING,
    "include_reasoning": Capability.REASONING,
    "structured_outputs": Capability.JSON_SCHEMA_OUTPUT,
    "response_format": Capability.JSON_OBJECT_OUTPUT,
}

_MODALITY_CAPABILITIES = {
    "image": Capability.IMAGE_INPUT,
    "audio": Capability.AUDIO_INPUT,
    "video": Capability.VIDEO_INPUT,
}


async def _openrouter_catalog(transport: Transport) -> CatalogResult:
    data = await transport.get_json("/models")
    items = data.get("data")
    if not isinstance(items, list):
        raise ModelCatalogError("OpenRouter catalog has no data array")

    entries: list[CatalogEntry] = []
    malformed = 0
    for item in items:
        entry = _openrouter_entry(item)
        if entry is None:
            malformed += 1
        else:
            entries.append(entry)

    return CatalogResult(
        source="openrouter-catalog",
        ok=malformed == 0,
        entries=entries,
        partial=bool(malformed and entries),
        error_type="malformed_response" if malformed else None,
        error_message=(
            f"{malformed} catalog entr{'y' if malformed == 1 else 'ies'} were unreadable"
            if malformed
            else None
        ),
    )


def _openrouter_entry(item: object) -> CatalogEntry | None:
    if not isinstance(item, dict):
        return None
    model_id = item.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        return None

    architecture = item.get("architecture") if isinstance(item.get("architecture"), dict) else {}
    parameters = {
        value for value in (item.get("supported_parameters") or []) if isinstance(value, str)
    }
    modalities = {
        value for value in (architecture.get("input_modalities") or []) if isinstance(value, str)
    }

    claims: dict[Capability, CapabilityStatus] = {}
    for parameter, capability in _OPENROUTER_PARAMETER_CAPABILITIES.items():
        supported = parameter in parameters
        # Never downgrade a capability already claimed supported by a sibling parameter name
        # ("tools" and "tool_choice" both imply tool calling).
        if supported:
            claims[capability] = CapabilityStatus.SUPPORTED
        else:
            claims.setdefault(capability, CapabilityStatus.UNSUPPORTED)
    for modality, capability in _MODALITY_CAPABILITIES.items():
        claims[capability] = (
            CapabilityStatus.SUPPORTED if modality in modalities else CapabilityStatus.UNSUPPORTED
        )
    if "text" in modalities or not modalities:
        claims[Capability.TEXT] = CapabilityStatus.SUPPORTED

    pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
    try:
        model = RemoteModel(
            id=model_id,
            display_name=item.get("name") or model_id,
            owned_by=model_id.split("/", 1)[0] if "/" in model_id else None,
            context_window=_positive_int(item.get("context_length")),
            canonical_slug=item.get("canonical_slug"),
            output_modalities=architecture.get("output_modalities") or [],
        )
    except (TypeError, ValueError):
        return None

    return CatalogEntry(
        model=model,
        claims=claims,
        deprecated=bool(item.get("deprecated")),
        revision=item.get("canonical_slug")
        if isinstance(item.get("canonical_slug"), str)
        else None,
        chat_capable=None,
        prompt_price=_price(pricing.get("prompt")),
        completion_price=_price(pricing.get("completion")),
    )


def _price(value: object) -> float | None:
    """Prices arrive as decimal strings. Parsed, never rounded — they drive a max-price filter."""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- Ollama


#: Ollama's ``/api/show`` reports a ``capabilities`` array. Only names it documents are mapped; an
#: unrecognised name is left alone rather than guessed at.
_OLLAMA_CAPABILITIES = {
    "completion": Capability.TEXT,
    "tools": Capability.TOOL_CALLING,
    "thinking": Capability.REASONING,
    "vision": Capability.IMAGE_INPUT,
    "embedding": None,
    "insert": None,
}


async def _ollama_tags_show(transport: Transport) -> CatalogResult:
    tags = await transport.get_json("/api/tags")
    listed = tags.get("models")
    if not isinstance(listed, list):
        raise ModelCatalogError("Ollama /api/tags has no models array")

    loaded_names = await _ollama_loaded(transport)

    base: list[tuple[str, dict[str, Any]]] = []
    malformed = 0
    for item in listed:
        if not isinstance(item, dict):
            malformed += 1
            continue
        name = item.get("name") or item.get("model")
        if not isinstance(name, str) or not name.strip():
            malformed += 1
            continue
        base.append((name, item))

    described = await _ollama_describe([name for name, _ in base[:_SHOW_LIMIT]], transport)

    entries: list[CatalogEntry] = []
    show_failures = 0
    for name, item in base:
        detail = item.get("details") if isinstance(item.get("details"), dict) else {}
        show = described.get(name)
        if show is None and name in {n for n, _ in base[:_SHOW_LIMIT]}:
            show_failures += 1

        claims: dict[Capability, CapabilityStatus] = {}
        for reported in (show or {}).get("capabilities") or []:
            capability = _OLLAMA_CAPABILITIES.get(reported) if isinstance(reported, str) else None
            if capability is not None:
                claims[capability] = CapabilityStatus.SUPPORTED

        try:
            model = RemoteModel(
                id=name,
                display_name=name,
                owned_by=detail.get("family") if isinstance(detail.get("family"), str) else None,
                context_window=_ollama_context(show),
                parameter_size=detail.get("parameter_size"),
                quantization=detail.get("quantization_level"),
                size_bytes=_positive_int(item.get("size")),
                modified_at=item.get("modified_at"),
            )
        except (TypeError, ValueError):
            malformed += 1
            continue

        entries.append(
            CatalogEntry(
                model=model,
                claims=claims,
                # The digest is the model's real identity: two pulls of the same tag can be
                # different weights, and a capability verified against one is not evidence about
                # the other (spec §9).
                revision=item.get("digest") if isinstance(item.get("digest"), str) else None,
                loaded=name in loaded_names,
                chat_capable=None,
            )
        )

    degraded = bool(malformed or show_failures)
    return CatalogResult(
        source="ollama-tags-show",
        ok=not degraded,
        entries=entries,
        partial=degraded and bool(entries),
        error_type="malformed_response" if degraded else None,
        error_message=(
            f"{show_failures} model(s) could not be described and are listed without capability claims"
            if show_failures
            else (
                f"{malformed} unreadable entr{'y' if malformed == 1 else 'ies'}"
                if malformed
                else None
            )
        ),
    )


async def _ollama_loaded(transport: Transport) -> set[str]:
    """Which models are resident. A failure here means "unknown", never "none loaded"."""

    try:
        running = await transport.get_json("/api/ps")
    except (TransportError, ValueError):
        return set()
    names: set[str] = set()
    for item in running.get("models") or []:
        if isinstance(item, dict):
            name = item.get("name") or item.get("model")
            if isinstance(name, str) and name:
                names.add(name)
    return names


async def _ollama_describe(
    names: list[str], transport: Transport
) -> dict[str, dict[str, Any] | None]:
    """Fan out ``/api/show``, bounded, tolerating individual failures.

    A model whose ``show`` failed is still a model the user has. It is listed with *no* capability
    claims rather than dropped or given defaults — the honest states are "supported" and "we did not
    find out", and a failed metadata read is the second one.
    """

    semaphore = asyncio.Semaphore(_SHOW_CONCURRENCY)

    async def one(name: str) -> tuple[str, dict[str, Any] | None]:
        async with semaphore:
            try:
                return name, await transport.post_json("/api/show", {"model": name})
            except (TransportError, ValueError):
                return name, None

    results = await asyncio.gather(*(one(name) for name in names))
    return dict(results)


def _ollama_context(show: dict[str, Any] | None) -> int | None:
    """Find the context length in ``model_info``, whose keys are architecture-prefixed."""

    info = (show or {}).get("model_info")
    if not isinstance(info, dict):
        return None
    for key, value in info.items():
        if isinstance(key, str) and key.endswith(".context_length"):
            length = _positive_int(value)
            if length is not None:
                return length
    return None


# --------------------------------------------------------------------------- LM Studio


#: LM Studio's native REST path has moved between builds, and an OpenAI-compatible ``/v1/models`` is
#: always available. So the paths are *probed* in richest-first order and the one that answered is
#: reported, rather than a single path being hardcoded and failing on half the installs.
_LMSTUDIO_PATHS = ("/api/v0/models", "/api/v1/models", "/v1/models")

_LMSTUDIO_NON_CHAT_TYPES = {"embeddings", "embedding", "vlm-vision-encoder"}


async def _lmstudio_native(transport: Transport) -> CatalogResult:
    last_error: TransportError | None = None
    for path in _LMSTUDIO_PATHS:
        try:
            data = await transport.get_json(path)
        except TransportError as exc:
            # A missing path is the expected outcome for the ones this build does not serve; a real
            # failure (auth, unreachable) is kept and re-raised if nothing answers.
            last_error = exc
            if exc.error_type in {ErrorType.MODEL_NOT_FOUND, ErrorType.INVALID_REQUEST}:
                continue
            raise
        entries, malformed = _lmstudio_entries(data)
        return CatalogResult(
            source=f"lmstudio-native ({path})",
            ok=malformed == 0,
            entries=entries,
            partial=bool(malformed and entries),
            error_type="malformed_response" if malformed else None,
            error_message=(
                f"{malformed} unreadable entr{'y' if malformed == 1 else 'ies'}"
                if malformed
                else None
            ),
        )
    if last_error is not None:
        raise last_error
    raise ModelCatalogError("LM Studio served no model endpoint")


def _lmstudio_entries(data: dict[str, Any]) -> tuple[list[CatalogEntry], int]:
    items = data.get("data")
    if not isinstance(items, list):
        raise ModelCatalogError("LM Studio model catalog has no data array")

    entries: list[CatalogEntry] = []
    malformed = 0
    for item in items:
        if not isinstance(item, dict):
            malformed += 1
            continue
        model_id = item.get("id") or item.get("key")
        if not isinstance(model_id, str) or not model_id.strip():
            malformed += 1
            continue

        model_type = item.get("type") if isinstance(item.get("type"), str) else None
        is_chat = None if model_type is None else model_type not in _LMSTUDIO_NON_CHAT_TYPES
        claims: dict[Capability, CapabilityStatus] = {}
        if is_chat is False:
            # An embeddings model demonstrably cannot hold a conversation. This is the one negative
            # a listing may assert, because the *type* is the provider stating it outright.
            claims[Capability.TEXT] = CapabilityStatus.UNSUPPORTED
        if model_type == "vlm":
            claims[Capability.IMAGE_INPUT] = CapabilityStatus.SUPPORTED

        try:
            model = RemoteModel(
                id=model_id,
                display_name=item.get("display_name") or model_id,
                owned_by=item.get("publisher") if isinstance(item.get("publisher"), str) else None,
                context_window=_positive_int(
                    item.get("max_context_length") or item.get("context_length")
                ),
                architecture=item.get("arch"),
                quantization=item.get("quantization"),
                model_type=model_type,
            )
        except (TypeError, ValueError):
            malformed += 1
            continue

        entries.append(
            CatalogEntry(
                model=model,
                claims=claims,
                revision=_lmstudio_revision(item),
                loaded=_lmstudio_loaded(item),
                chat_capable=is_chat,
            )
        )
    return entries, malformed


def _lmstudio_revision(item: dict[str, Any]) -> str | None:
    """Architecture plus quantization: the pair that actually changes a local model's behaviour."""

    parts = [
        str(value)
        for value in (item.get("arch"), item.get("quantization"))
        if isinstance(value, (str, int)) and str(value)
    ]
    return "/".join(parts) if parts else None


def _lmstudio_loaded(item: dict[str, Any]) -> bool | None:
    state = item.get("state")
    if not isinstance(state, str):
        return None
    return state == "loaded"


# --------------------------------------------------------------------------- Gemini


async def _gemini_models(transport: Transport) -> CatalogResult:
    """Delegate to the Gemini adapter's paged reader, which already handles page tokens.

    Imported here rather than at module scope: the adapter imports this module's siblings, and a
    top-level import would make the provider package's import order significant.
    """

    from .gemini_interactions import GeminiInteractionsAdapter

    adapter = GeminiInteractionsAdapter(api_key=None, transport=transport)
    models = await adapter.list_models()
    entries = []
    for model in models:
        extra = model.model_dump()
        methods = extra.get("supported_generation_methods") or []
        claims: dict[Capability, CapabilityStatus] = {}
        if isinstance(methods, list) and methods:
            # Google lists which methods a model serves. That is the provider stating what the model
            # can be *asked* to do, so it is recorded — as a catalog claim, outranked by a probe.
            if any("generateContent" in str(method) for method in methods):
                claims[Capability.TEXT] = CapabilityStatus.SUPPORTED
            if any("countTokens" in str(method) for method in methods):
                claims[Capability.TOKEN_COUNTING] = CapabilityStatus.SUPPORTED
        entries.append(
            CatalogEntry(
                model=model,
                claims=claims,
                revision=extra.get("version") if isinstance(extra.get("version"), str) else None,
            )
        )
    return CatalogResult(source="gemini-models", ok=True, entries=entries)


# --------------------------------------------------------------------------- curated manifests


#: Versioned curated manifests, for providers with no programmatic catalog (spec §16.4).
#:
#: Deliberately plain data and deliberately *thin*: model ids and context windows only, no capability
#: claims beyond text. A manifest is the weakest evidence source and the one most likely to be wrong
#: after a provider ships a new model, so it exists to give the wizard a starting list — not to
#: answer questions a probe should answer. Runtime documentation scraping is explicitly not done
#: here (spec §16.4): a parser aimed at someone's docs page breaks silently and invents models.
_CURATED: dict[str, list[dict[str, Any]]] = {
    "qwen": [
        {"id": "qwen-max", "display_name": "Qwen Max", "context_window": 32768},
        {"id": "qwen-plus", "display_name": "Qwen Plus", "context_window": 131072},
        {"id": "qwen-turbo", "display_name": "Qwen Turbo", "context_window": 1000000},
        {"id": "qwen-flash", "display_name": "Qwen Flash", "context_window": 1000000},
    ],
}


def _curated(provider_type: str) -> CatalogResult:
    manifest = _CURATED.get(provider_type)
    if not manifest:
        return CatalogResult(
            source="curated-catalog",
            ok=True,
            manual_only=True,
            manifest_version=CURATED_MANIFEST_VERSION,
            error_message=f"no curated manifest for {provider_type!r}; enter a model id manually",
        )
    entries: list[CatalogEntry] = []
    for item in manifest:
        model = RemoteModel(
            id=item["id"],
            display_name=item.get("display_name") or item["id"],
            context_window=item.get("context_window"),
        )
        entries.append(
            CatalogEntry(
                model=model,
                # Only text, and only as a preset. Anything more would be a guess wearing the
                # clothes of a finding.
                claims={Capability.TEXT: CapabilityStatus.SUPPORTED},
                revision=CURATED_MANIFEST_VERSION,
                source=EvidenceSource.CURATED_PRESET,
                claim_detail=f"curated manifest {CURATED_MANIFEST_VERSION}",
            )
        )
    result = CatalogResult(
        source="curated-catalog",
        ok=True,
        entries=entries,
        manifest_version=CURATED_MANIFEST_VERSION,
    )
    return result


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
