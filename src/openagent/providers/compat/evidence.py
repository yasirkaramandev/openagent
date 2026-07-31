"""Capability evidence: what we know about a model, and how we came to know it (spec §9).

A capability flag on its own is a claim with no author. "This model supports tool calling" is a
very different statement depending on whether it came from a live probe against the configured
credential, from the provider's own catalog, from a fixture recorded once, or from a preset someone
typed while reading documentation. They are wrong in different ways and they go stale differently:
a catalog entry can advertise a capability the deployment does not have, a curated preset ages the
moment a provider ships a new model revision, and a live probe is the only one that observed the
thing actually happening.

So capability values are not stored bare. Each one carries its source, when it was observed, and
which probe/provider/model revision produced it — which is what makes it possible to say "verified
live" in the wizard and mean it, and to invalidate exactly the right rows when a credential is
rotated or a model revision changes.

``None`` continues to mean *not yet determined*, and is never upgraded to ``True`` by a profile
preset. A preset describes how to *talk* to a provider; it cannot testify about a model.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from ...core.models import utcnow


class Capability(str, Enum):
    """The capability surface v0.2 reasons about (spec §9)."""

    TEXT = "text"
    STREAMING = "streaming"
    TOOL_CALLING = "tool_calling"
    PARALLEL_TOOL_CALLING = "parallel_tool_calling"
    STREAMED_TOOL_ARGUMENTS = "streamed_tool_arguments"
    STRUCTURED_OUTPUT = "structured_output"
    JSON_OBJECT_OUTPUT = "json_object_output"
    JSON_SCHEMA_OUTPUT = "json_schema_output"
    REASONING = "reasoning"
    REASONING_EFFORT = "reasoning_effort"
    IMAGE_INPUT = "image_input"
    AUDIO_INPUT = "audio_input"
    VIDEO_INPUT = "video_input"
    SYSTEM_PROMPT = "system_prompt"
    TOKEN_COUNTING = "token_counting"
    SERVER_SIDE_SESSION = "server_side_session"
    CLIENT_HISTORY_RESUME = "client_history_resume"
    CONTEXT_WINDOW = "context_window"
    MAX_OUTPUT_TOKENS = "max_output_tokens"


class EvidenceSource(str, Enum):
    """Where a capability claim came from, strongest first.

    The ordering is the whole point: a live probe beats a catalog, a catalog beats a recorded
    fixture, a fixture beats someone's curated guess. ``MANUAL_OVERRIDE`` sits outside the ranking —
    see :func:`stronger`.
    """

    LIVE_PROBE = "live_probe"
    PROVIDER_CATALOG = "provider_catalog"
    VERIFIED_FIXTURE = "verified_fixture"
    CURATED_PRESET = "curated_preset"
    #: What an older build believed, carried across by migration 0016. The weakest automatic
    #: source by construction: it records a v0.1 boolean, not something observed under the current
    #: probe definition. Recording those as LIVE_PROBE would have been a lie with consequences —
    #: live probe is the strongest automatic source, so a v0.1 guess would have outranked every
    #: real catalog reading from then on.
    #:
    #: This member was missing while migration 0016 already wrote ``legacy_migration`` into the
    #: source column, so every row the migration produced raised ValueError on read.
    LEGACY_MIGRATION = "legacy_migration"
    MANUAL_OVERRIDE = "manual_override"


_RANK = {
    EvidenceSource.LIVE_PROBE: 40,
    EvidenceSource.PROVIDER_CATALOG: 30,
    EvidenceSource.VERIFIED_FIXTURE: 20,
    EvidenceSource.CURATED_PRESET: 10,
    EvidenceSource.LEGACY_MIGRATION: 5,
}

#: A human deliberately saying "yes it does, I checked" outranks every automatic source. It is also
#: the only source that can be wrong in a way no probe can correct, which is why it is recorded as
#: its own source rather than being written in as a fake probe result.
_MANUAL_RANK = 50


class CapabilityStatus(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class CapabilityEvidence(BaseModel):
    """One capability claim, with its provenance (spec §9).

    Carries no secrets: a probe records *that* a request succeeded, never the request, the response
    body, or the credential that authorized it.
    """

    model_config = ConfigDict(extra="forbid")

    capability: Capability
    status: CapabilityStatus = CapabilityStatus.UNKNOWN
    source: EvidenceSource = EvidenceSource.CURATED_PRESET
    observed_at: datetime = Field(default_factory=utcnow)
    #: Bumped when the probe's own logic changes, so old verdicts can be re-earned rather than
    #: inherited by a probe that now asks a different question.
    probe_version: int = 1
    #: The provider's advertised API/server version at observation time, when it reports one.
    provider_version: str | None = None
    #: The model's revision/snapshot id, when the provider exposes one. A capability verified
    #: against `gpt-x-2026-01-01` says nothing about `gpt-x-2026-06-01`.
    model_revision: str | None = None
    #: Short, operator-facing note. Never a raw provider payload.
    detail: str = ""

    @property
    def rank(self) -> int:
        if self.source is EvidenceSource.MANUAL_OVERRIDE:
            return _MANUAL_RANK
        return _RANK.get(self.source, 0)

    @property
    def is_determined(self) -> bool:
        return self.status is not CapabilityStatus.UNKNOWN


def stronger(left: CapabilityEvidence, right: CapabilityEvidence) -> CapabilityEvidence:
    """The claim that should win when two sources disagree.

    Rank first; on a tie, the more recent observation. An UNKNOWN never displaces a determined
    claim regardless of rank — a probe that failed to answer has not disproved anything, and
    treating it as an answer is how a transient network error silently marks a working model as
    incapable.
    """

    if left.is_determined and not right.is_determined:
        return left
    if right.is_determined and not left.is_determined:
        return right
    if left.rank != right.rank:
        return left if left.rank > right.rank else right
    return left if left.observed_at >= right.observed_at else right


class CapabilityLedger(BaseModel):
    """Every capability claim known for one model, keyed by capability.

    A ledger rather than a flat set of booleans, so the wizard can show *why* a badge is lit and
    Doctor can report how stale the answer is.
    """

    model_config = ConfigDict(extra="forbid")

    entries: dict[Capability, CapabilityEvidence] = Field(default_factory=dict)

    def record(self, evidence: CapabilityEvidence) -> CapabilityLedger:
        """Merge one claim in, keeping whichever survives :func:`stronger`."""

        existing = self.entries.get(evidence.capability)
        winner = evidence if existing is None else stronger(existing, evidence)
        return CapabilityLedger(entries={**self.entries, evidence.capability: winner})

    def status(self, capability: Capability) -> CapabilityStatus:
        entry = self.entries.get(capability)
        return entry.status if entry else CapabilityStatus.UNKNOWN

    def supports(self, capability: Capability) -> bool | None:
        """``True``/``False`` when determined, ``None`` when not yet known.

        Callers must treat ``None`` as "do not know" and not as ``False``: refusing to send a tool
        because nothing has probed for tool support yet is a different bug from refusing because
        the model genuinely lacks it.
        """

        status = self.status(capability)
        if status is CapabilityStatus.UNKNOWN:
            return None
        return status is CapabilityStatus.SUPPORTED

    def source_of(self, capability: Capability) -> EvidenceSource | None:
        entry = self.entries.get(capability)
        return entry.source if entry else None

    def invalidate(self, *, sources: set[EvidenceSource] | None = None) -> CapabilityLedger:
        """Drop claims that a change has made untrustworthy.

        Used when a credential is rotated (a probe verified with the old key proves nothing about
        the new one) or a model revision moves. Manual overrides survive by default: a human's
        explicit statement is not invalidated by a key rotation.
        """

        drop = sources or {EvidenceSource.LIVE_PROBE, EvidenceSource.PROVIDER_CATALOG}
        return CapabilityLedger(
            entries={cap: e for cap, e in self.entries.items() if e.source not in drop}
        )
