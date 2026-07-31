"""Add-Agent wizard v2: the decisions, separated from the screen (spec §22).

The v1 wizard put its logic in the Textual screen, which meant the interesting rules — when a step is
reachable, what a capability badge is allowed to claim, what happens when a catalog cannot be read —
were only testable by driving widgets. This module holds those rules as plain objects so they can be
tested directly, and the screen renders them.

Four rules carry most of the weight.

**A badge names its evidence.** "Tools" lit because OpenRouter's catalog said so and "Tools" lit
because a probe watched it happen are different claims, and a user choosing a model for an agent that
must call tools needs to know which one they have. So a badge carries its source and its age, and an
unverified badge is visually distinct from a verified one rather than absent.

**A catalog that cannot be read is not an empty catalog.** Four explicit ways forward — retry, use the
cached list, type an id, use the provider's default — because presenting an empty list as authoritative
is how a working provider looks broken.

**A probe gate is a gate.** If the user said the agent needs tools and the probe could not establish
tool support, creation does not silently proceed. It requires an explicit override, and the override is
recorded.

**Server-side state is disclosed at the point of choosing it.** Not in a settings page nobody opens:
the step where a user turns it on is the step that says the provider will retain the conversation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from ..core.models import Protocol, utcnow
from ..providers.compat.evidence import (
    Capability,
    CapabilityLedger,
    CapabilityStatus,
    EvidenceSource,
)


class WizardStep(str, Enum):
    """The twelve steps spec §22 defines, in order."""

    RUNTIME = "runtime"
    PROVIDER = "provider"
    AUTH = "auth"
    REGION = "region"
    MODEL_DISCOVERY = "model-discovery"
    CAPABILITY_FILTER = "capability-filter"
    PERMISSION = "permission"
    SANDBOX = "sandbox"
    RESUME = "resume"
    PROBE = "probe"
    REVIEW = "review"
    CREATE = "create"


#: Steps that only apply to one runtime. A CLI agent has no region or auth step of its own (the CLI owns
#: its credential), and an API agent has no sandbox step (there is no subprocess to sandbox).
_API_ONLY = {WizardStep.REGION, WizardStep.MODEL_DISCOVERY, WizardStep.CAPABILITY_FILTER}
_CLI_ONLY = {WizardStep.SANDBOX}


def steps_for(runtime: str | None) -> tuple[WizardStep, ...]:
    """The steps that actually apply, so the wizard never shows a step with nothing in it."""

    if runtime is None:
        return (WizardStep.RUNTIME,)
    if runtime == "cli":
        return tuple(step for step in WizardStep if step not in _API_ONLY)
    return tuple(step for step in WizardStep if step not in _CLI_ONLY)


# --------------------------------------------------------------------------- badges


#: The badge row spec §22.3 defines, in display order.
BADGE_CAPABILITIES: tuple[Capability, ...] = (
    Capability.TEXT,
    Capability.STREAMING,
    Capability.TOOL_CALLING,
    Capability.PARALLEL_TOOL_CALLING,
    Capability.STREAMED_TOOL_ARGUMENTS,
    Capability.REASONING,
    Capability.JSON_OBJECT_OUTPUT,
    Capability.JSON_SCHEMA_OUTPUT,
    Capability.IMAGE_INPUT,
    Capability.AUDIO_INPUT,
    Capability.VIDEO_INPUT,
    Capability.CONTEXT_WINDOW,
    Capability.SERVER_SIDE_SESSION,
    Capability.CLIENT_HISTORY_RESUME,
)

_BADGE_LABELS = {
    Capability.TEXT: "Text",
    Capability.STREAMING: "Streaming",
    Capability.TOOL_CALLING: "Tools",
    Capability.PARALLEL_TOOL_CALLING: "Parallel Tools",
    Capability.STREAMED_TOOL_ARGUMENTS: "Streamed Tool Arguments",
    Capability.REASONING: "Reasoning",
    Capability.JSON_OBJECT_OUTPUT: "JSON Object",
    Capability.JSON_SCHEMA_OUTPUT: "JSON Schema",
    Capability.IMAGE_INPUT: "Image",
    Capability.AUDIO_INPUT: "Audio",
    Capability.VIDEO_INPUT: "Video",
    Capability.CONTEXT_WINDOW: "Context",
    Capability.SERVER_SIDE_SESSION: "Server Resume",
    Capability.CLIENT_HISTORY_RESUME: "Client Resume",
}

#: How each source is described in a tooltip. The wording matters: a user deciding whether to trust a
#: badge is deciding whether to trust *this sentence*.
_SOURCE_WORDING = {
    EvidenceSource.LIVE_PROBE: "verified by a live probe against this credential",
    EvidenceSource.PROVIDER_CATALOG: "reported by the provider's catalog, not verified here",
    EvidenceSource.VERIFIED_FIXTURE: "from a recorded fixture, not verified against your account",
    EvidenceSource.CURATED_PRESET: "from a curated preset; the weakest source, and it ages",
    EvidenceSource.MANUAL_OVERRIDE: "set manually by you",
}

#: Live-probe evidence older than this is shown as aged. Not refreshed automatically: re-probing spends
#: the user's quota, and doing that as a side effect of opening a wizard step is not a decision to make
#: for them.
STALE_AFTER = timedelta(days=30)


@dataclass(frozen=True)
class Badge:
    """One capability badge, with the provenance a user needs to weigh it."""

    capability: Capability
    label: str
    status: CapabilityStatus
    source: EvidenceSource | None
    observed_at: datetime | None
    probe_version: int | None
    model_revision: str | None
    stale: bool

    @property
    def verified(self) -> bool:
        """Lit *and* observed. A catalog claim is lit and not verified."""

        return (
            self.status is CapabilityStatus.SUPPORTED
            and self.source is EvidenceSource.LIVE_PROBE
            and not self.stale
        )

    @property
    def lit(self) -> bool:
        return self.status is CapabilityStatus.SUPPORTED

    @property
    def unknown(self) -> bool:
        return self.status is CapabilityStatus.UNKNOWN

    def tooltip(self) -> str:
        """The sentence the user reads before trusting the badge."""

        if self.unknown or self.source is None:
            return f"{self.label}: not established for this model. Run the capability probe to settle it."
        wording = _SOURCE_WORDING.get(self.source, "source unknown")
        verb = "supported" if self.lit else "not supported"
        parts = [f"{self.label}: {verb} — {wording}"]
        if self.observed_at is not None:
            parts.append(f"observed {self.observed_at.date().isoformat()}")
        if self.stale:
            parts.append("this evidence has aged; re-probe to confirm")
        if self.model_revision:
            parts.append(f"model revision {self.model_revision}")
        return "; ".join(parts)


def build_badges(ledger: CapabilityLedger, *, now: datetime | None = None) -> list[Badge]:
    """Every badge in the row, including the ones nothing has established.

    Absent badges are not omitted. A missing badge is indistinguishable from an unsupported one at a
    glance, and "we do not know" is the most common truthful answer.
    """

    moment = now or utcnow()
    badges: list[Badge] = []
    for capability in BADGE_CAPABILITIES:
        evidence = ledger.entries.get(capability)
        if evidence is None:
            badges.append(
                Badge(
                    capability=capability,
                    label=_BADGE_LABELS[capability],
                    status=CapabilityStatus.UNKNOWN,
                    source=None,
                    observed_at=None,
                    probe_version=None,
                    model_revision=None,
                    stale=False,
                )
            )
            continue
        observed = evidence.observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=moment.tzinfo)
        stale = evidence.source is EvidenceSource.LIVE_PROBE and (moment - observed) > STALE_AFTER
        badges.append(
            Badge(
                capability=capability,
                label=_BADGE_LABELS[capability],
                status=evidence.status,
                source=evidence.source,
                observed_at=observed,
                probe_version=evidence.probe_version,
                model_revision=evidence.model_revision,
                stale=stale,
            )
        )
    return badges


# --------------------------------------------------------------------------- catalog fallback


class CatalogFallback(str, Enum):
    """The ways forward when a catalog could not be read (spec §22.4).

    All four are offered because they are genuinely different: retry addresses a transient failure,
    the cache addresses an outage, a manual id addresses a provider with no catalog at all, and the
    provider default addresses a user who does not care which model.
    """

    RETRY = "retry"
    USE_CACHED = "use-cached"
    MANUAL_ID = "manual-id"
    PROVIDER_DEFAULT = "provider-default"


@dataclass
class CatalogPresentation:
    """What the model-discovery step shows, given how the catalog attempt went."""

    models: list[Any] = field(default_factory=list)
    #: The offered ways forward. Empty when the catalog read fine.
    fallbacks: tuple[CatalogFallback, ...] = ()
    message: str = ""
    #: True when the list shown is authoritative and complete.
    authoritative: bool = True

    @property
    def blocked(self) -> bool:
        """Whether the user must choose a fallback before continuing."""

        return not self.models and bool(self.fallbacks)


def present_catalog(result: Any, *, cached_available: bool = False) -> CatalogPresentation:
    """Turn a :class:`~...providers.model_catalog.CatalogResult` into what the step should show.

    The four outcomes get four presentations, which is the entire reason this function exists — a
    single "here is the list" path is what turns an unreadable catalog into an empty one.
    """

    if result.manual_only:
        return CatalogPresentation(
            models=list(result.entries),
            fallbacks=(CatalogFallback.MANUAL_ID, CatalogFallback.PROVIDER_DEFAULT),
            message=(
                "this provider has no listable catalog; the list below is a starting point, and you "
                "can type any model id"
            ),
            authoritative=False,
        )

    if not result.ok and not result.entries:
        fallbacks = [CatalogFallback.RETRY]
        if cached_available:
            fallbacks.append(CatalogFallback.USE_CACHED)
        fallbacks += [CatalogFallback.MANUAL_ID, CatalogFallback.PROVIDER_DEFAULT]
        return CatalogPresentation(
            models=[],
            fallbacks=tuple(fallbacks),
            message=(
                f"the model catalog could not be read ({result.error_type}): "
                f"{result.error_message}. This is not the same as the provider having no models."
            ),
            authoritative=False,
        )

    if result.partial:
        return CatalogPresentation(
            models=list(result.entries),
            fallbacks=(CatalogFallback.RETRY, CatalogFallback.MANUAL_ID),
            message=(
                f"{len(result.entries)} model(s) were readable and some entries were not; the list is "
                f"incomplete"
            ),
            authoritative=False,
        )

    return CatalogPresentation(
        models=list(result.entries),
        message=f"{len(result.entries)} model(s)",
        authoritative=True,
    )


# --------------------------------------------------------------------------- probe gate


@dataclass(frozen=True)
class RequiredCapabilities:
    """What the user said this agent must be able to do."""

    tools: bool = False
    reasoning: bool = False
    vision: bool = False

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        wanted: list[Capability] = []
        if self.tools:
            wanted.append(Capability.TOOL_CALLING)
        if self.reasoning:
            wanted.append(Capability.REASONING)
        if self.vision:
            wanted.append(Capability.IMAGE_INPUT)
        return tuple(wanted)

    @property
    def any_required(self) -> bool:
        return bool(self.capabilities)


@dataclass
class ProbeGate:
    """Whether creation may proceed, given what the probe established (spec §22.5)."""

    #: Capabilities the user required that the probe found unsupported.
    unsupported: tuple[Capability, ...] = ()
    #: Capabilities the user required that remain unestablished.
    unverified: tuple[Capability, ...] = ()
    #: Set when the probe could not run at all.
    blocked_reason: str = ""

    @property
    def satisfied(self) -> bool:
        return not self.unsupported and not self.unverified and not self.blocked_reason

    @property
    def requires_override(self) -> bool:
        """Whether an explicit, recorded override is needed to continue."""

        return not self.satisfied

    def message(self) -> str:
        if self.satisfied:
            return "every required capability was verified"
        if self.unsupported:
            names = ", ".join(capability.value for capability in self.unsupported)
            return (
                f"this model does not support {names}, which you marked as required. Creating the "
                f"agent anyway means it will fail at the first attempt to use it."
            )
        if self.blocked_reason:
            return (
                f"the capability probe could not run ({self.blocked_reason}), so nothing about this "
                f"model has been verified."
            )
        names = ", ".join(capability.value for capability in self.unverified)
        return (
            f"{names} could not be verified for this model. It may work; nothing here has seen it "
            f"work."
        )


def evaluate_probe_gate(
    ledger: CapabilityLedger,
    required: RequiredCapabilities,
    *,
    probe_blocked_reason: str = "",
) -> ProbeGate:
    """Compare what the user requires against what is actually established.

    ``UNKNOWN`` and ``UNSUPPORTED`` are kept apart deliberately. Both stop the gate, and they stop it
    with different messages, because "this model cannot do it" and "nobody has checked" lead a user to
    different decisions.
    """

    if not required.any_required:
        return ProbeGate()

    unsupported: list[Capability] = []
    unverified: list[Capability] = []
    for capability in required.capabilities:
        supported = ledger.supports(capability)
        if supported is False:
            unsupported.append(capability)
        elif supported is None:
            unverified.append(capability)
    return ProbeGate(
        unsupported=tuple(unsupported),
        unverified=tuple(unverified),
        blocked_reason=probe_blocked_reason if (unverified and probe_blocked_reason) else "",
    )


# --------------------------------------------------------------------------- disclosures


@dataclass(frozen=True)
class Disclosure:
    """Something the user is told at the moment they choose it, not afterwards."""

    key: str
    title: str
    body: str


#: Server-side state disclosure, per provider (spec §22.6). Worded concretely — "Google retains the
#: conversation" rather than "state is stored remotely" — because the abstract phrasing is what lets
#: someone enable it without registering what it means.
_SERVER_STATE_DISCLOSURES = {
    "gemini": Disclosure(
        key="server-state",
        title="Google will retain this conversation",
        body=(
            "With provider-managed state, each turn is stored by Google and resumed by its interaction "
            "id. OpenAgent's local-only default keeps the conversation on this machine instead. "
            "Resuming later depends on Google still holding it."
        ),
    ),
    "qwen": Disclosure(
        key="server-state",
        title="Alibaba Model Studio will retain this conversation",
        body=(
            "Provider-managed state stores each response server-side and resumes by its id. The "
            "local-only default keeps the conversation on this machine."
        ),
    ),
    "lmstudio": Disclosure(
        key="server-state",
        title="LM Studio will hold this conversation in its own process",
        body=(
            "State stays on this machine either way. Provider-managed state means LM Studio owns it, "
            "so it is lost when the server restarts and is not covered by OpenAgent's backups."
        ),
    ),
}


def server_state_disclosure(provider_type: str) -> Disclosure | None:
    """The disclosure for enabling provider-held state, or ``None`` if the provider has none."""

    return _SERVER_STATE_DISCLOSURES.get(provider_type)


def required_disclosures(
    *, provider_type: str | None, server_state_enabled: bool, insecure_http: bool = False
) -> list[Disclosure]:
    """Every disclosure this configuration owes the user before Create."""

    out: list[Disclosure] = []
    if provider_type and server_state_enabled:
        disclosure = server_state_disclosure(provider_type)
        if disclosure is not None:
            out.append(disclosure)
    if insecure_http:
        out.append(
            Disclosure(
                key="insecure-http",
                title="This connection is not encrypted",
                body=(
                    "The endpoint is plain HTTP at a non-loopback address, so the API key and every "
                    "prompt cross the network in cleartext. Anything on the path can read them."
                ),
            )
        )
    return out


# --------------------------------------------------------------------------- review


@dataclass
class ReviewSummary:
    """What the review step shows before Create (spec §22.11). Secret-free by construction."""

    runtime: str
    provider: str | None = None
    protocol: Protocol | None = None
    region: str | None = None
    workspace_id: str | None = None
    model: str | None = None
    cli_type: str | None = None
    permission_profile: str = "safe-edit"
    sandbox: bool = False
    resume_mode: str | None = None
    server_state_enabled: bool = False
    credential_source: str | None = None
    #: Verified badges only, so review does not restate catalog claims as findings.
    verified_capabilities: tuple[str, ...] = ()
    unverified_capabilities: tuple[str, ...] = ()
    disclosures: tuple[Disclosure, ...] = ()
    probe_override_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """A rendering-friendly projection. Deliberately has no field a secret could occupy."""

        return {
            "runtime": self.runtime,
            "provider": self.provider,
            "protocol": self.protocol.value if self.protocol else None,
            "region": self.region,
            "workspace_id": self.workspace_id,
            "model": self.model,
            "cli_type": self.cli_type,
            "permission_profile": self.permission_profile,
            "sandbox": self.sandbox,
            "resume_mode": self.resume_mode,
            "server_state_enabled": self.server_state_enabled,
            # The *source* of the credential, never the credential. "keychain" is information; the
            # key is not something a review screen has any reason to hold.
            "credential_source": self.credential_source,
            "verified_capabilities": list(self.verified_capabilities),
            "unverified_capabilities": list(self.unverified_capabilities),
            "disclosures": [
                {"key": d.key, "title": d.title, "body": d.body} for d in self.disclosures
            ],
            "probe_override_reason": self.probe_override_reason,
        }


def build_review(
    *,
    runtime: str,
    badges: list[Badge],
    provider: str | None = None,
    protocol: Protocol | None = None,
    region: str | None = None,
    workspace_id: str | None = None,
    model: str | None = None,
    cli_type: str | None = None,
    permission_profile: str = "safe-edit",
    sandbox: bool = False,
    resume_mode: str | None = None,
    server_state_enabled: bool = False,
    credential_source: str | None = None,
    insecure_http: bool = False,
    probe_override_reason: str | None = None,
) -> ReviewSummary:
    """Assemble the review, splitting verified capabilities from merely-claimed ones."""

    verified = tuple(badge.label for badge in badges if badge.verified)
    unverified = tuple(badge.label for badge in badges if badge.lit and not badge.verified)
    return ReviewSummary(
        runtime=runtime,
        provider=provider,
        protocol=protocol,
        region=region,
        workspace_id=workspace_id,
        model=model,
        cli_type=cli_type,
        permission_profile=permission_profile,
        sandbox=sandbox,
        resume_mode=resume_mode,
        server_state_enabled=server_state_enabled,
        credential_source=credential_source,
        verified_capabilities=verified,
        unverified_capabilities=unverified,
        disclosures=tuple(
            required_disclosures(
                provider_type=provider,
                server_state_enabled=server_state_enabled,
                insecure_http=insecure_http,
            )
        ),
        probe_override_reason=probe_override_reason,
    )
