"""Doctor's section structure (spec §21).

``openagent doctor`` produced a flat list of checks. A flat list is readable at ten checks and stops
being readable at sixty, which is roughly where v0.2 lands it — nine providers, six CLIs, per-model
capability evidence, session artifacts. Worse, a flat list has no way to say "the database is fine and
the providers are not": every line has equal weight, so the one that matters is the one you happen to
read.

So checks are grouped into the eleven sections §21 names, and each section carries its own worst
status. That makes "what is broken" answerable at a glance and "why" answerable by expanding one
section.

Two rules the grouping follows:

* **Every check lands in exactly one section.** A check with no section is invisible in a sectioned
  view, which is worse than a noisy flat list. Anything unrecognised goes to ``INSTALLATION`` and is
  reported as unclassified rather than dropped.
* **A section with nothing to report says so.** "No providers configured" is a finding; an absent
  section reads as "we did not look".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .doctor_service import FAIL, OK, WARN, Check


class DoctorSection(str, Enum):
    """The eleven sections spec §21 requires, in the order they are rendered."""

    INSTALLATION = "installation"
    DATABASE = "database"
    CREDENTIALS = "credentials"
    PROVIDERS = "providers"
    MODELS = "models"
    CLI_ADAPTERS = "cli-adapters"
    SESSIONS = "sessions"
    WORKSPACES = "workspaces"
    SECURITY_BACKEND = "security-backend"
    UPDATER = "updater"
    RELEASE_CHANNEL = "release-channel"

    @property
    def display_name(self) -> str:
        """Human-readable section heading.

        Not called ``title``: ``DoctorSection`` subclasses ``str``, so a ``title`` property would
        shadow ``str.title()`` — a real name collision, and one that silently changes what
        ``section.title`` means depending on whether you remember the enum is a string.
        """

        return _TITLES[self]


_TITLES = {
    DoctorSection.INSTALLATION: "Installation",
    DoctorSection.DATABASE: "Database",
    DoctorSection.CREDENTIALS: "Credentials",
    DoctorSection.PROVIDERS: "Providers",
    DoctorSection.MODELS: "Models",
    DoctorSection.CLI_ADAPTERS: "CLI adapters",
    DoctorSection.SESSIONS: "Sessions",
    DoctorSection.WORKSPACES: "Workspaces",
    DoctorSection.SECURITY_BACKEND: "Security backend",
    DoctorSection.UPDATER: "Updater",
    DoctorSection.RELEASE_CHANNEL: "Release channel",
}

#: Substrings that assign a check to a section, most specific first. Matching on the check's *name*
#: rather than requiring every producer to declare a section keeps the existing checks working
#: unchanged — and an unmatched name is reported, not silently dropped, so a new check that lands in
#: the fallback is visible rather than lost.
_ROUTES: tuple[tuple[str, DoctorSection], ...] = (
    # --- most specific first. Two traps live here and both were caught by tests:
    #     * "release" contains "lease", so a bare "lease" route captured "Release channel";
    #     * a bare "update" route and a bare "cli" route both match "CLI update check", so the more
    #       specific pair has to be resolved before either.
    ("release channel", DoctorSection.RELEASE_CHANNEL),
    ("release", DoctorSection.RELEASE_CHANNEL),
    ("cli update", DoctorSection.CLI_ADAPTERS),
    ("active lease", DoctorSection.DATABASE),
    ("turn lease", DoctorSection.DATABASE),
    # --- database
    ("schema", DoctorSection.DATABASE),
    ("sqlite", DoctorSection.DATABASE),
    ("database", DoctorSection.DATABASE),
    ("migration", DoctorSection.DATABASE),
    ("event", DoctorSection.DATABASE),
    ("journal", DoctorSection.DATABASE),
    ("integrity", DoctorSection.DATABASE),
    # --- security backend / credentials
    ("keychain", DoctorSection.SECURITY_BACKEND),
    ("keyring", DoctorSection.SECURITY_BACKEND),
    ("credential", DoctorSection.CREDENTIALS),
    ("api key", DoctorSection.CREDENTIALS),
    # --- models before providers: "Model capabilities: x" also contains neither, but a provider
    #     route on "provider" would otherwise capture "Provider model probe".
    ("capabilit", DoctorSection.MODELS),
    ("model", DoctorSection.MODELS),
    ("probe", DoctorSection.MODELS),
    # --- providers
    ("provider", DoctorSection.PROVIDERS),
    # --- CLI adapters
    ("cli", DoctorSection.CLI_ADAPTERS),
    ("codex", DoctorSection.CLI_ADAPTERS),
    ("claude", DoctorSection.CLI_ADAPTERS),
    ("gemini", DoctorSection.CLI_ADAPTERS),
    ("antigravity", DoctorSection.CLI_ADAPTERS),
    ("qwen", DoctorSection.CLI_ADAPTERS),
    ("kimi", DoctorSection.CLI_ADAPTERS),
    # --- sessions
    ("session", DoctorSection.SESSIONS),
    ("resume", DoctorSection.SESSIONS),
    ("continuation", DoctorSection.SESSIONS),
    ("run", DoctorSection.SESSIONS),
    # --- workspaces
    ("workspace", DoctorSection.WORKSPACES),
    ("worktree", DoctorSection.WORKSPACES),
    ("git", DoctorSection.WORKSPACES),
    ("directory", DoctorSection.WORKSPACES),
    ("openagent.md", DoctorSection.WORKSPACES),
    # --- updater (OpenAgent's own), then installation as the fallthrough
    ("update", DoctorSection.UPDATER),
    ("channel", DoctorSection.RELEASE_CHANNEL),
    ("version", DoctorSection.INSTALLATION),
    ("install", DoctorSection.INSTALLATION),
    ("path", DoctorSection.INSTALLATION),
    ("configuration", DoctorSection.INSTALLATION),
)

#: Status precedence. A section is only as healthy as its worst check: a green summary over one failing
#: row is the specific thing a sectioned view must not do.
_RANK = {OK: 0, WARN: 1, FAIL: 2}


def section_for(check_name: str) -> DoctorSection:
    """Route one check to its section.

    ``INSTALLATION`` is the fallback rather than a dedicated "other" section: a real section is
    something a user scans, and an "other" bucket is where checks go to be ignored.
    """

    lowered = check_name.lower()
    for token, section in _ROUTES:
        if token in lowered:
            return section
    return DoctorSection.INSTALLATION


def is_unclassified(check_name: str) -> bool:
    """Whether a check reached its section only by falling through every route."""

    lowered = check_name.lower()
    return not any(token in lowered for token, _ in _ROUTES)


@dataclass
class SectionReport:
    """One section's checks and its rolled-up status."""

    section: DoctorSection
    checks: list[Check] = field(default_factory=list)
    #: Names that only matched the fallback route, so a new unrouted check is visible.
    unclassified: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if not self.checks:
            # An empty section is not healthy and not broken. WARN, so it is visible: "no providers
            # configured" is a finding, and OK would hide it.
            return WARN
        return max((check.status for check in self.checks), key=lambda value: _RANK.get(value, 1))

    @property
    def title(self) -> str:
        return self.section.display_name

    @property
    def counts(self) -> dict[str, int]:
        counts = {OK: 0, WARN: 0, FAIL: 0}
        for check in self.checks:
            counts[check.status] = counts.get(check.status, 0) + 1
        return counts

    def summary(self) -> str:
        if not self.checks:
            return "nothing to report"
        counts = self.counts
        parts = [f"{counts[OK]} ok"]
        if counts[WARN]:
            parts.append(f"{counts[WARN]} warning")
        if counts[FAIL]:
            parts.append(f"{counts[FAIL]} failing")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section.value,
            "title": self.title,
            "status": self.status,
            "summary": self.summary(),
            "counts": self.counts,
            "checks": [check.to_dict() for check in self.checks],
            "unclassified": list(self.unclassified),
        }


def group_checks(checks: list[Check]) -> list[SectionReport]:
    """Group a flat check list into the eleven sections, in render order.

    Every section is present in the result even when it has no checks, because an absent section reads
    as "we did not look" and that is the one thing a diagnostic must never imply.
    """

    reports = {section: SectionReport(section=section) for section in DoctorSection}
    for check in checks:
        section = section_for(check.name)
        reports[section].checks.append(check)
        if is_unclassified(check.name):
            reports[section].unclassified.append(check.name)
    return [reports[section] for section in DoctorSection]


def overall_status(reports: list[SectionReport]) -> str:
    """The worst status across sections, ignoring sections that are merely empty."""

    populated = [report.status for report in reports if report.checks]
    if not populated:
        return WARN
    return max(populated, key=lambda value: _RANK.get(value, 1))


def failing_sections(reports: list[SectionReport]) -> list[DoctorSection]:
    return [report.section for report in reports if report.status == FAIL]


def to_dict(reports: list[SectionReport]) -> dict[str, Any]:
    """The whole sectioned report, for ``doctor --json``."""

    return {
        "overall": overall_status(reports),
        "failing_sections": [section.value for section in failing_sections(reports)],
        "sections": [report.to_dict() for report in reports],
    }


# --------------------------------------------------------------------------- v0.2 checks


def cli_discovery_checks(reports: list[Any]) -> list[Check]:
    """Turn :class:`~..runtimes.cli.discovery.CliDiscoveryReport`s into Doctor checks (spec §21.5).

    Status mapping is the honest one: a CLI that is not installed is *not* a failure — the user simply
    does not have it — while one that is installed and unusable is, because the wizard would offer it.
    """

    from ..runtimes.cli.discovery import DiscoveryStatus

    checks: list[Check] = []
    for report in reports:
        if report.status is DiscoveryStatus.NOT_INSTALLED:
            status = OK
        elif report.status is DiscoveryStatus.UNSUPPORTED:
            status = FAIL
        elif report.warnings:
            status = WARN
        else:
            status = OK
        checks.append(
            Check(
                name=f"CLI {report.display_name}",
                status=status,
                detail=report.status_label,
                data=report.to_dict(),
            )
        )
    return checks


def migration_hold_check(status: dict[str, Any]) -> Check:
    """Report whether migrations 0015-0017 are active, and why not (spec §23).

    A held migration set is a WARN rather than an OK: it is a real, temporary state of the build that
    an operator should be able to see without reading the source, and it clears itself the moment the
    base revision lands.
    """

    if status.get("registered"):
        return Check(
            name="v0.2 schema migrations",
            status=OK,
            detail=f"{', '.join(status['revisions'])} are in the migration chain",
            data=status,
        )
    return Check(
        name="v0.2 schema migrations",
        status=WARN,
        detail=str(status.get("reason") or "held back"),
        data=status,
    )


def capability_evidence_check(*, model_id: str, ledger: Any, stale_after_days: int = 30) -> Check:
    """Report a model's capability evidence with its provenance and age (spec §21.4).

    Staleness is a warning, never a silent refresh: re-probing costs a request against the user's
    credential, and doing that as a side effect of running a diagnostic is not Doctor's decision.
    """

    from datetime import timedelta

    from ..core.models import utcnow
    from ..providers.compat.evidence import CapabilityStatus, EvidenceSource

    entries = getattr(ledger, "entries", {}) or {}
    if not entries:
        return Check(
            name=f"Model capabilities: {model_id}",
            status=WARN,
            detail="no capability evidence recorded; the wizard will show every badge as unverified",
            data={"model": model_id, "capabilities": {}},
        )

    now = utcnow()
    threshold = timedelta(days=stale_after_days)
    stale: list[str] = []
    presented: dict[str, Any] = {}
    for capability, evidence in entries.items():
        observed = evidence.observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=now.tzinfo)
        age = now - observed
        if age > threshold and evidence.source is EvidenceSource.LIVE_PROBE:
            stale.append(capability.value)
        presented[capability.value] = {
            "status": evidence.status.value,
            "source": evidence.source.value,
            "observed_at": observed.isoformat(),
            "age_days": max(age.days, 0),
            "probe_version": evidence.probe_version,
            "model_revision": evidence.model_revision,
        }

    determined = sum(
        1 for evidence in entries.values() if evidence.status is not CapabilityStatus.UNKNOWN
    )
    detail = f"{determined} of {len(entries)} capabilities determined"
    if stale:
        detail += (
            f"; probe evidence older than {stale_after_days} days for {', '.join(sorted(stale))}"
        )
    return Check(
        name=f"Model capabilities: {model_id}",
        status=WARN if stale else OK,
        detail=detail,
        data={"model": model_id, "capabilities": presented, "stale": sorted(stale)},
    )


def session_resume_check(*, run_id: str, decision: Any) -> Check:
    """Report whether one recorded session can still be resumed (spec §21.6)."""

    if decision.refused:
        return Check(
            name=f"Session resume: {run_id}",
            status=WARN,
            detail=decision.summary(),
            data=decision.to_dict(),
        )
    return Check(
        name=f"Session resume: {run_id}",
        status=OK,
        detail=decision.summary(),
        data=decision.to_dict(),
    )


def provider_connection_check(*, describe: dict[str, Any], catalog: Any | None) -> Check:
    """One provider row's connection facts (spec §21.3).

    A plain-HTTP non-loopback endpoint is a FAIL, not a warning: a credential is crossing the network
    in cleartext, and that is not a thing to note in passing.
    """

    label = describe.get("label") or describe.get("provider_type") or "provider"
    tls = bool(describe.get("tls"))
    loopback = bool(describe.get("loopback"))
    data = dict(describe)

    if not tls and not loopback:
        return Check(
            name=f"Provider {label}",
            status=FAIL,
            detail="reached over plain HTTP at a non-loopback address; the credential is sent in cleartext",
            data=data,
        )

    if catalog is None:
        return Check(
            name=f"Provider {label}",
            status=WARN,
            detail="the model catalog has not been read for this connection",
            data=data,
        )

    data["catalog"] = {
        "source": catalog.source,
        "ok": catalog.ok,
        "partial": catalog.partial,
        "manual_only": catalog.manual_only,
        "models": len(catalog.entries),
        "error_type": catalog.error_type,
    }
    if catalog.manual_only:
        return Check(name=f"Provider {label}", status=OK, detail=catalog.summary(), data=data)
    if not catalog.ok and not catalog.entries:
        return Check(name=f"Provider {label}", status=FAIL, detail=catalog.summary(), data=data)
    if catalog.partial:
        return Check(name=f"Provider {label}", status=WARN, detail=catalog.summary(), data=data)
    return Check(name=f"Provider {label}", status=OK, detail=catalog.summary(), data=data)
