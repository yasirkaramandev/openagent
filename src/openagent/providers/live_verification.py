"""Verification status for a provider or CLI (spec §28.4).

The whole reason this is a typed enum and not a boolean is the gap between "we tested it and it
works" and "we did not test it". Those collapse into the same green tick the moment a report is
allowed to say ``PASS``, and once collapsed they never separate again — a provider that has never
been reached is indistinguishable from one that has, and the roadmap says both are done.

So a status is one of six things, and only one of them is a pass. In particular
:attr:`VerificationStatus.BLOCKED_BY_CREDENTIAL` is *not* a pass: a live test that never ran
because no key was configured has established nothing at all about the provider, and reporting it
as anything other than untested is the specific dishonesty this module exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..core.models import utcnow


class VerificationStatus(str, Enum):
    """How thoroughly a provider or CLI has actually been checked (spec §28.4)."""

    #: A real request reached the provider and behaved as the adapter expects. The only pass.
    LIVE_VERIFIED = "LIVE_VERIFIED"
    #: The adapter's mapping is proven against recorded fixtures. Says nothing about the live wire
    #: format — a fixture recorded in June cannot notice a schema change in July.
    FIXTURE_VERIFIED = "FIXTURE_VERIFIED"
    #: The model catalog was read; no inference request was ever made.
    CATALOG_ONLY = "CATALOG_ONLY"
    #: Nothing was run.
    NOT_TESTED = "NOT_TESTED"
    #: A live check was configured but no usable credential was present, so it did not run. This is
    #: not a pass and must never be rendered as one.
    BLOCKED_BY_CREDENTIAL = "BLOCKED_BY_CREDENTIAL"
    #: The provider or local service could not be reached, so the check is inconclusive rather than
    #: failed — the adapter has not been shown to be wrong.
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"

    @property
    def is_pass(self) -> bool:
        """Only a live check passes. Everything else is a degree of not-knowing."""

        return self is VerificationStatus.LIVE_VERIFIED

    @property
    def is_evidence_of_correctness(self) -> bool:
        """Whether this status justifies claiming the adapter works at all.

        Fixtures prove the mapping and a catalog read proves reachability and auth, so both are
        real evidence — just weaker than a live turn, and weaker in a way the report has to keep
        visible.
        """

        return self in {
            VerificationStatus.LIVE_VERIFIED,
            VerificationStatus.FIXTURE_VERIFIED,
            VerificationStatus.CATALOG_ONLY,
        }


@dataclass(frozen=True)
class VerificationResult:
    """One provider or CLI's verification state, with the reason behind it."""

    subject: str
    status: VerificationStatus = VerificationStatus.NOT_TESTED
    #: Which checks actually ran, e.g. ``("list_models", "stream_text")``.
    checks: tuple[str, ...] = ()
    detail: str = ""
    observed_at: datetime = field(default_factory=utcnow)

    @property
    def reportable(self) -> str:
        """The line a status report prints. Never abbreviates a non-pass into ``PASS``."""

        suffix = f" — {self.detail}" if self.detail else ""
        return f"{self.subject}: {self.status.value}{suffix}"


def blocked_by_credential(subject: str, env_var: str) -> VerificationResult:
    """The result to record when a live check could not run for want of a key.

    Named rather than constructed inline so that the reason is always attached: a bare
    ``BLOCKED_BY_CREDENTIAL`` with no detail is one editing pass away from being mistaken for a
    transient failure.
    """

    return VerificationResult(
        subject=subject,
        status=VerificationStatus.BLOCKED_BY_CREDENTIAL,
        detail=f"no credential in {env_var}; live verification did not run",
    )
