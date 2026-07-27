"""Verification status (spec §28.4).

Small module, one job: keep "we tested it" and "we did not test it" from collapsing into the same
green tick. These tests are the guard on that.
"""

from __future__ import annotations

import pytest

from openagent.providers.live_verification import (
    VerificationResult,
    VerificationStatus,
    blocked_by_credential,
)

pytestmark = pytest.mark.unit


def test_only_a_live_check_is_a_pass():
    passing = [status for status in VerificationStatus if status.is_pass]
    assert passing == [VerificationStatus.LIVE_VERIFIED]


def test_a_missing_credential_is_not_a_pass():
    # The specific dishonesty this module exists to prevent: a check that never ran, reported green.
    assert not VerificationStatus.BLOCKED_BY_CREDENTIAL.is_pass
    assert not VerificationStatus.BLOCKED_BY_CREDENTIAL.is_evidence_of_correctness


def test_an_unreachable_provider_is_inconclusive_not_failed():
    # Nothing has shown the adapter to be wrong, so this must not read as a failure either.
    assert not VerificationStatus.PROVIDER_UNAVAILABLE.is_pass
    assert not VerificationStatus.PROVIDER_UNAVAILABLE.is_evidence_of_correctness


@pytest.mark.parametrize(
    "status",
    [
        VerificationStatus.LIVE_VERIFIED,
        VerificationStatus.FIXTURE_VERIFIED,
        VerificationStatus.CATALOG_ONLY,
    ],
)
def test_weaker_evidence_still_counts_as_evidence(status: VerificationStatus):
    assert status.is_evidence_of_correctness


def test_not_tested_is_not_evidence():
    assert not VerificationStatus.NOT_TESTED.is_evidence_of_correctness


def test_the_reported_line_never_abbreviates_a_non_pass():
    result = blocked_by_credential("gemini-api", "OPENAGENT_LIVE_GEMINI_API_KEY")

    assert "BLOCKED_BY_CREDENTIAL" in result.reportable
    assert "PASS" not in result.reportable
    assert "OPENAGENT_LIVE_GEMINI_API_KEY" in result.reportable


def test_a_blocked_result_always_carries_its_reason():
    # A bare BLOCKED_BY_CREDENTIAL is one editing pass from being mistaken for a flake.
    assert blocked_by_credential("x", "Y_KEY").detail


def test_the_default_status_is_untested():
    assert VerificationResult(subject="anything").status is VerificationStatus.NOT_TESTED


def test_a_live_result_reports_the_checks_that_ran():
    result = VerificationResult(
        subject="gemini-api",
        status=VerificationStatus.LIVE_VERIFIED,
        checks=("list_models", "stream_text"),
        detail="7 checks",
    )

    assert result.status.is_pass
    assert result.checks == ("list_models", "stream_text")
    assert result.reportable == "gemini-api: LIVE_VERIFIED — 7 checks"
