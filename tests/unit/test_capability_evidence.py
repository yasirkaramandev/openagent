"""Capability evidence and its precedence rules (spec §9).

The property under test throughout: a capability is only "supported" when something actually
established that, and the ledger can always say *what* established it.
"""

from __future__ import annotations

from datetime import timedelta

from openagent.core.models import utcnow
from openagent.providers.compat.evidence import (
    Capability,
    CapabilityEvidence,
    CapabilityLedger,
    CapabilityStatus,
    EvidenceSource,
    stronger,
)


def _ev(
    capability=Capability.TOOL_CALLING,
    status=CapabilityStatus.SUPPORTED,
    source=EvidenceSource.LIVE_PROBE,
    **kw,
):
    return CapabilityEvidence(capability=capability, status=status, source=source, **kw)


# ------------------------------------------------------------------ precedence


def test_live_probe_beats_catalog_beats_fixture_beats_preset() -> None:
    order = [
        EvidenceSource.LIVE_PROBE,
        EvidenceSource.PROVIDER_CATALOG,
        EvidenceSource.VERIFIED_FIXTURE,
        EvidenceSource.CURATED_PRESET,
    ]
    ranks = [_ev(source=s).rank for s in order]
    assert ranks == sorted(ranks, reverse=True), ranks


def test_manual_override_outranks_every_automatic_source() -> None:
    manual = _ev(source=EvidenceSource.MANUAL_OVERRIDE, status=CapabilityStatus.SUPPORTED)
    probe = _ev(source=EvidenceSource.LIVE_PROBE, status=CapabilityStatus.UNSUPPORTED)
    assert stronger(manual, probe) is manual


def test_a_more_recent_observation_wins_within_the_same_source() -> None:
    old = _ev(observed_at=utcnow() - timedelta(days=3), status=CapabilityStatus.UNSUPPORTED)
    new = _ev(observed_at=utcnow(), status=CapabilityStatus.SUPPORTED)
    assert stronger(old, new) is new


def test_an_unknown_never_displaces_a_determined_claim() -> None:
    """A probe that failed to answer has not disproved anything.

    Without this rule a transient network error during a re-probe would overwrite a verified
    "supports tools" with "unknown", and the model would quietly stop being offered for tool use.
    """

    determined = _ev(source=EvidenceSource.CURATED_PRESET, status=CapabilityStatus.SUPPORTED)
    unknown = _ev(source=EvidenceSource.LIVE_PROBE, status=CapabilityStatus.UNKNOWN)
    assert stronger(determined, unknown) is determined
    assert stronger(unknown, determined) is determined


# ------------------------------------------------------------------ ledger


def test_unrecorded_capability_is_none_not_false() -> None:
    """ "Not tested" and "does not support" are different answers and must not be conflated."""

    ledger = CapabilityLedger()
    assert ledger.supports(Capability.TOOL_CALLING) is None
    assert ledger.status(Capability.TOOL_CALLING) is CapabilityStatus.UNKNOWN
    assert ledger.source_of(Capability.TOOL_CALLING) is None


def test_recording_a_claim_makes_it_readable_with_its_source() -> None:
    ledger = CapabilityLedger().record(
        _ev(source=EvidenceSource.PROVIDER_CATALOG, status=CapabilityStatus.SUPPORTED)
    )
    assert ledger.supports(Capability.TOOL_CALLING) is True
    assert ledger.source_of(Capability.TOOL_CALLING) is EvidenceSource.PROVIDER_CATALOG


def test_a_probe_overrides_a_catalog_claim() -> None:
    ledger = CapabilityLedger().record(
        _ev(source=EvidenceSource.PROVIDER_CATALOG, status=CapabilityStatus.SUPPORTED)
    )
    ledger = ledger.record(
        _ev(source=EvidenceSource.LIVE_PROBE, status=CapabilityStatus.UNSUPPORTED)
    )
    assert ledger.supports(Capability.TOOL_CALLING) is False
    assert ledger.source_of(Capability.TOOL_CALLING) is EvidenceSource.LIVE_PROBE


def test_a_catalog_claim_does_not_override_a_probe() -> None:
    """The catalog advertises the model; the probe observed this deployment of it."""

    ledger = CapabilityLedger().record(
        _ev(source=EvidenceSource.LIVE_PROBE, status=CapabilityStatus.UNSUPPORTED)
    )
    ledger = ledger.record(
        _ev(source=EvidenceSource.PROVIDER_CATALOG, status=CapabilityStatus.SUPPORTED)
    )
    assert ledger.supports(Capability.TOOL_CALLING) is False


def test_capabilities_are_tracked_independently() -> None:
    ledger = CapabilityLedger()
    ledger = ledger.record(_ev(Capability.TOOL_CALLING, status=CapabilityStatus.SUPPORTED))
    ledger = ledger.record(_ev(Capability.REASONING, status=CapabilityStatus.UNSUPPORTED))
    assert ledger.supports(Capability.TOOL_CALLING) is True
    assert ledger.supports(Capability.REASONING) is False
    assert ledger.supports(Capability.IMAGE_INPUT) is None


def test_recording_returns_a_new_ledger_and_leaves_the_original_alone() -> None:
    original = CapabilityLedger()
    updated = original.record(_ev())
    assert original.supports(Capability.TOOL_CALLING) is None
    assert updated.supports(Capability.TOOL_CALLING) is True


# ------------------------------------------------------------------ invalidation


def test_credential_rotation_drops_probe_and_catalog_claims() -> None:
    """A verdict earned with the old key proves nothing about the new one."""

    ledger = CapabilityLedger()
    ledger = ledger.record(_ev(Capability.TOOL_CALLING, source=EvidenceSource.LIVE_PROBE))
    ledger = ledger.record(_ev(Capability.REASONING, source=EvidenceSource.PROVIDER_CATALOG))
    ledger = ledger.record(_ev(Capability.TEXT, source=EvidenceSource.VERIFIED_FIXTURE))

    rotated = ledger.invalidate()
    assert rotated.supports(Capability.TOOL_CALLING) is None
    assert rotated.supports(Capability.REASONING) is None
    assert rotated.supports(Capability.TEXT) is True  # a fixture does not depend on a credential


def test_manual_overrides_survive_invalidation() -> None:
    ledger = CapabilityLedger().record(
        _ev(Capability.TOOL_CALLING, source=EvidenceSource.MANUAL_OVERRIDE)
    )
    assert ledger.invalidate().supports(Capability.TOOL_CALLING) is True


def test_evidence_carries_revision_context_for_staleness() -> None:
    """A verdict against one model revision says nothing about the next."""

    evidence = _ev(model_revision="gpt-x-2026-01-01", provider_version="v1", probe_version=2)
    assert evidence.model_revision == "gpt-x-2026-01-01"
    assert evidence.provider_version == "v1"
    assert evidence.probe_version == 2


def test_evidence_forbids_unknown_fields_so_a_payload_cannot_smuggle_secrets() -> None:
    import pydantic
    import pytest

    with pytest.raises(pydantic.ValidationError):
        CapabilityEvidence(capability=Capability.TEXT, api_key="sk-secret")  # type: ignore[call-arg]
