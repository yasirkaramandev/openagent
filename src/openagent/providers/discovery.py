"""Model discovery + capability probing + cache (spec §25, §15, §16).

Model IDs must never be hardcoded — providers rotate them constantly (spec §15, §25). This module
lists a provider's models and probes a specific model's capabilities, caching results with a TTL.

A *catalog listing is not a capability claim* (spec §14.3): hosted catalogs such as NVIDIA Build mix
chat, embedding, rerank and vision models behind one ``/models`` endpoint, and reaching ``/models``
does not even prove the API key works (the catalog may be public). Only :func:`probe_agent_model`,
which actually exercises the selected model, may be used to call a model agent-compatible.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..core.errors import ErrorType
from ..core.models import ModelCapabilities, ModelProfile, RemoteModel
from .base import (
    Message,
    NormalizedModelRequest,
    ProviderAdapter,
    Role,
    collect,
)

DEFAULT_TTL = timedelta(days=7)

#: Bump when the probe's request shapes or success rules change, so cached results from an older
#: definition are not trusted (spec §16).
PROBE_VERSION = "1"

#: A probe must be small and bounded (spec §15.1): tiny max_tokens, short prompts, strict timeout.
PROBE_MAX_TOKENS = 64
PROBE_TIMEOUT = 30.0

#: Probe outcome categories — each maps to an honest, actionable user-facing message.
PROBE_VERIFIED = "verified"                  # text + streaming + tool calling all observed
PROBE_PARTIAL = "partial"                    # text works; tool calling not verified
PROBE_INCOMPATIBLE = "incompatible"          # 4xx/422 — wrong request shape for this model
PROBE_NOT_FOUND = "not_found"                # 404 — model gone from the catalog
PROBE_UNAUTHORIZED = "unauthorized"          # 401/403 — the key was rejected
PROBE_RATE_LIMITED = "rate_limited"          # 429
PROBE_ASYNC_UNSUPPORTED = "async_unsupported"  # 202 + request id (spec §15.5)
PROBE_UNREACHABLE = "unreachable"            # transport/timeout


@dataclass
class AgentModelProbe:
    """What a real probe of one model actually proved (spec §15).

    ``agent_compatible`` is True **only** when text, streaming and tool calling were each observed —
    never inferred from the model's name or its presence in a catalog.
    """

    model: str
    capabilities: ModelCapabilities
    agent_compatible: bool
    category: str
    detail: str = ""
    tested_at: datetime | None = None
    probe_version: str = PROBE_VERSION

    def to_dict(self) -> dict:
        caps = self.capabilities
        return {
            "model": self.model,
            "text": bool(caps.text),
            "streaming": caps.streaming,
            "tool_calling": caps.tool_calling,
            "agent_compatible": self.agent_compatible,
            "category": self.category,
            "detail": self.detail,
            "tested_at": self.tested_at.isoformat() if self.tested_at else None,
            "probe_version": self.probe_version,
        }

    def message(self) -> str:
        """The honest, user-facing verdict for this probe result (spec §15.2-§15.5)."""

        return _PROBE_MESSAGES.get(self.category, self.detail or "the model could not be validated")


_PROBE_MESSAGES = {
    PROBE_VERIFIED: "Verified Agent Compatible",
    PROBE_PARTIAL: (
        "Text generation works, but tool calling was not verified. This model may answer questions "
        "but may not operate OpenAgent tools."
    ),
    PROBE_INCOMPATIBLE: "This catalog model is not compatible with OpenAgent's chat-agent runtime.",
    PROBE_NOT_FOUND: "Model was not found or is no longer available. Refresh the catalog.",
    PROBE_UNAUTHORIZED: "The API key was rejected.",
    PROBE_RATE_LIMITED: "Rate limit or quota reached.",
    PROBE_ASYNC_UNSUPPORTED: (
        "Asynchronous invocation is not supported by the OpenAgent chat runtime yet."
    ),
    PROBE_UNREACHABLE: "The model endpoint could not be reached.",
}

_ERROR_CATEGORIES = {
    ErrorType.ASYNC_UNSUPPORTED.value: PROBE_ASYNC_UNSUPPORTED,
    ErrorType.INVALID_REQUEST.value: PROBE_INCOMPATIBLE,
    ErrorType.MODEL_NOT_FOUND.value: PROBE_NOT_FOUND,
    ErrorType.AUTHENTICATION_FAILED.value: PROBE_UNAUTHORIZED,
    ErrorType.PERMISSION_DENIED.value: PROBE_UNAUTHORIZED,
    ErrorType.PROVIDER_RATE_LIMITED.value: PROBE_RATE_LIMITED,
    ErrorType.CONTEXT_LIMIT.value: PROBE_INCOMPATIBLE,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def probe_agent_model(
    adapter: ProviderAdapter, model_id: str, *, timeout: float = PROBE_TIMEOUT
) -> AgentModelProbe:
    """Exercise ``model_id`` and report ONLY what was observed (spec §15.1).

    Three bounded requests — a tiny non-streaming completion, the same request streamed, and a
    tool-enabled request — mirroring :func:`~openagent.providers.base.default_probe`'s shapes. Each is
    wrapped in a strict timeout so a stalled endpoint cannot hang the caller, and a capability is
    claimed only when actually demonstrated. A transport error is classified into an honest category
    (unauthorized / not found / incompatible / async / rate limited) rather than a bare "failed".
    """

    text_req = NormalizedModelRequest(
        model=model_id,
        system="You are a probe. Reply with a single short word.",
        messages=[Message(role=Role.USER, content="Say ok.")],
        max_tokens=16, stream=False,
    )
    try:
        result = await asyncio.wait_for(collect(adapter.stream_response(text_req)), timeout)
    except asyncio.TimeoutError:
        return AgentModelProbe(model_id, ModelCapabilities(text=False), False, PROBE_UNREACHABLE,
                               "the model did not respond within the probe timeout", _now())
    except Exception as exc:  # noqa: BLE001 - any failure -> capabilities unknown, assert nothing
        return AgentModelProbe(model_id, ModelCapabilities(text=False), False, PROBE_UNREACHABLE,
                               str(exc), _now())
    if result.is_error:
        category = _ERROR_CATEGORIES.get(result.error_type or "", PROBE_UNREACHABLE)
        return AgentModelProbe(model_id, ModelCapabilities(text=False), False, category,
                               result.error_message or "", _now())

    caps = ModelCapabilities(text=bool(result.text), streaming=None, tool_calling=None)

    stream_req = text_req.model_copy(update={"stream": True})
    try:
        sres = await asyncio.wait_for(collect(adapter.stream_response(stream_req)), timeout)
        if not sres.is_error and sres.text:
            caps.streaming = True
    except Exception:  # noqa: BLE001 - leave streaming unknown, never True
        pass

    tool_req = NormalizedModelRequest(
        model=model_id,
        messages=[Message(role=Role.USER, content="Call the ping tool with value 1.")],
        tools=[{
            "name": "ping", "description": "health probe",
            "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}},
        }],
        max_tokens=PROBE_MAX_TOKENS, stream=False,
    )
    try:
        tres = await asyncio.wait_for(collect(adapter.stream_response(tool_req)), timeout)
        if not tres.is_error and tres.tool_calls:
            caps.tool_calling = True
    except Exception:  # noqa: BLE001 - absence/err doesn't disprove support -> leave None
        pass

    # An OpenAgent API agent needs all three; anything less is honestly reported as partial (§15.2).
    agent_compatible = bool(caps.text and caps.streaming and caps.tool_calling)
    category = PROBE_VERIFIED if agent_compatible else (
        PROBE_PARTIAL if caps.text else PROBE_INCOMPATIBLE
    )
    return AgentModelProbe(model_id, caps, agent_compatible, category, "", _now())


async def discover_models(adapter: ProviderAdapter) -> list[RemoteModel]:
    """List models the provider exposes (empty list if it has no ``/models``)."""

    try:
        return await adapter.list_models()
    except Exception:  # noqa: BLE001 - discovery is best-effort
        return []


#: Substrings that *hint* a catalog entry may not be a chat model. These drive a **warning only**
#: (spec §14.3) — never a capability decision. A real probe is the only authority; guessing from a
#: name would either block a working model or bless a broken one.
NON_CHAT_HINTS = (
    "embed", "embedding", "rerank", "clip", "detector", "parse", "vision", "image", "video",
)


def looks_non_chat(model_id: str) -> bool:
    """Whether ``model_id`` *looks* like a non-chat model — for a warning, never a verdict (§14.3)."""

    lowered = model_id.lower()
    return any(hint in lowered for hint in NON_CHAT_HINTS)


def filter_models(
    models: Sequence[RemoteModel], *, search: str | None = None, owner: str | None = None,
) -> list[RemoteModel]:
    """Local, case-insensitive catalog filtering (spec §14.2, §17.3).

    Purely local so typing in the UI never triggers a network call. ``search`` matches the model id
    or display name; ``owner`` matches the publisher the catalog reported (``owned_by``).
    """

    result = list(models)
    if owner:
        needle = owner.strip().lower()
        result = [m for m in result if (m.owned_by or "").lower() == needle]
    if search:
        needle = search.strip().lower()
        result = [
            m for m in result
            if needle in m.id.lower() or needle in (m.display_name or "").lower()
        ]
    return result


def publishers(models: Sequence[RemoteModel]) -> list[str]:
    """The distinct publishers present in a catalog, sorted — for the publisher filter (§14.2)."""

    return sorted({m.owned_by for m in models if m.owned_by})


async def probe_capabilities(adapter: ProviderAdapter, model_id: str) -> ModelCapabilities:
    """Run the adapter's capability probe (spec §25.2)."""

    return await adapter.probe_model(model_id)


def capabilities_fresh(profile: ModelProfile, ttl: timedelta = DEFAULT_TTL) -> bool:
    """Whether a model's cached capabilities are still within the TTL (spec §25.3)."""

    if profile.capabilities_tested_at is None:
        return False
    tested = profile.capabilities_tested_at
    if tested.tzinfo is None:
        tested = tested.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - tested < ttl


def apply_probe(profile: ModelProfile, probed: ModelCapabilities) -> ModelProfile:
    """Merge probe results into a model profile and stamp the test time."""

    merged = profile.capabilities.merge(probed)
    return profile.model_copy(
        update={"capabilities": merged, "capabilities_tested_at": datetime.now(timezone.utc)}
    )
