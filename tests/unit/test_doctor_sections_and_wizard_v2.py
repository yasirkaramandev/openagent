"""Doctor's sections and the Add-Agent wizard's decisions (spec §21, §22).

Both modules exist to stop the same class of mistake: presenting an uncertain answer as a settled one.
Doctor does it by never letting a section summarize away a failing row; the wizard does it by making a
badge carry its evidence and a probe gate actually gate.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from openagent.core.models import Protocol, utcnow
from openagent.providers.compat.evidence import (
    Capability,
    CapabilityEvidence,
    CapabilityLedger,
    CapabilityStatus,
    EvidenceSource,
)
from openagent.providers.model_catalog import CatalogEntry, CatalogResult
from openagent.services.doctor_sections import (
    DoctorSection,
    capability_evidence_check,
    cli_discovery_checks,
    group_checks,
    migration_hold_check,
    overall_status,
    provider_connection_check,
    section_for,
    to_dict,
)
from openagent.services.doctor_service import FAIL, OK, WARN, Check
from openagent.tui.wizard_v2 import (
    STALE_AFTER,
    CatalogFallback,
    RequiredCapabilities,
    WizardStep,
    build_badges,
    build_review,
    evaluate_probe_gate,
    present_catalog,
    required_disclosures,
    server_state_disclosure,
    steps_for,
)


def ledger_with(**claims) -> CapabilityLedger:
    ledger = CapabilityLedger()
    for capability, (status, source) in claims.items():
        ledger = ledger.record(
            CapabilityEvidence(capability=capability, status=status, source=source)
        )
    return ledger


# =========================================================================== doctor sections


class TestSectionRouting:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("SQLite writable", DoctorSection.DATABASE),
            ("Database migration", DoctorSection.DATABASE),
            ("OS keychain available", DoctorSection.SECURITY_BACKEND),
            ("Provider credential: deepseek", DoctorSection.CREDENTIALS),
            ("Model capabilities: gpt-x", DoctorSection.MODELS),
            ("CLI Codex CLI", DoctorSection.CLI_ADAPTERS),
            ("Session resume: r1", DoctorSection.SESSIONS),
            ("Current directory is a Git repository", DoctorSection.WORKSPACES),
            # A CLI's own updater belongs with the CLI (spec §21.5 lists "update" there);
            # OpenAgent's updater is the separate Updater section.
            ("CLI update check", DoctorSection.CLI_ADAPTERS),
            ("OpenAgent update available", DoctorSection.UPDATER),
            ("Release channel", DoctorSection.RELEASE_CHANNEL),
            ("OpenAgent configuration", DoctorSection.INSTALLATION),
        ],
    )
    def test_checks_route_to_their_section(self, name: str, expected: DoctorSection) -> None:
        assert section_for(name) is expected

    def test_release_is_not_captured_by_the_lease_route(self) -> None:
        """ "Release" contains "lease". A substring table needs the specific entry first."""

        assert section_for("Release channel") is DoctorSection.RELEASE_CHANNEL
        assert section_for("Active leases") is DoctorSection.DATABASE

    def test_an_unrecognised_check_lands_somewhere_visible(self) -> None:
        """An unrouted check must not vanish; a dedicated "other" bucket is where things go to be ignored."""

        assert section_for("something entirely new") is DoctorSection.INSTALLATION

    def test_every_section_is_present_even_when_empty(self) -> None:
        """An absent section reads as "we did not look", which a diagnostic must never imply."""

        reports = group_checks([])
        assert [report.section for report in reports] == list(DoctorSection)

    def test_an_empty_section_warns_rather_than_reporting_healthy(self) -> None:
        reports = group_checks([])
        providers = next(r for r in reports if r.section is DoctorSection.PROVIDERS)
        assert providers.status == WARN
        assert providers.summary() == "nothing to report"


class TestSectionStatus:
    def test_a_section_is_only_as_healthy_as_its_worst_check(self) -> None:
        """The specific failure a sectioned view must not have: a green summary over a red row."""

        reports = group_checks(
            [
                Check("Provider a", OK),
                Check("Provider b", FAIL, "credential rejected"),
                Check("Provider c", OK),
            ]
        )
        providers = next(r for r in reports if r.section is DoctorSection.PROVIDERS)
        assert providers.status == FAIL
        assert providers.counts == {OK: 2, WARN: 0, FAIL: 1}

    def test_a_warning_does_not_mask_a_failure(self) -> None:
        reports = group_checks([Check("Provider a", WARN), Check("Provider b", FAIL)])
        assert next(r for r in reports if r.section is DoctorSection.PROVIDERS).status == FAIL

    def test_overall_status_ignores_merely_empty_sections(self) -> None:
        reports = group_checks([Check("SQLite writable", OK)])
        assert overall_status(reports) == OK

    def test_the_json_projection_names_the_failing_sections(self) -> None:
        reports = group_checks([Check("Provider a", FAIL), Check("SQLite writable", OK)])
        payload = to_dict(reports)
        assert payload["overall"] == FAIL
        assert "providers" in payload["failing_sections"]
        assert len(payload["sections"]) == len(DoctorSection)


class TestDoctorV2Checks:
    def test_a_not_installed_cli_is_not_a_failure(self) -> None:
        """The user simply does not have it. Reporting that red trains people to ignore red."""

        from openagent.runtimes.cli.discovery import (
            CliDiscoveryReport,
            DiscoveryStatus,
            get_descriptor,
        )

        report = CliDiscoveryReport(
            cli_type="gemini",
            display_name="Gemini CLI",
            descriptor=get_descriptor("gemini"),
            status=DiscoveryStatus.NOT_INSTALLED,
            status_label="Gemini CLI is not installed",
        )
        assert cli_discovery_checks([report])[0].status == OK

    def test_an_installed_but_unusable_cli_is_a_failure(self) -> None:
        """The wizard would offer it, and it cannot work."""

        from openagent.runtimes.cli.discovery import (
            CliDiscoveryReport,
            DiscoveryStatus,
            get_descriptor,
        )

        report = CliDiscoveryReport(
            cli_type="claude",
            display_name="Claude Code",
            descriptor=get_descriptor("claude"),
            status=DiscoveryStatus.UNSUPPORTED,
            status_label="installed, but not signed in",
            installed=True,
        )
        assert cli_discovery_checks([report])[0].status == FAIL

    def test_the_migration_hold_is_reported_as_a_warning_with_its_reason(self) -> None:
        from openagent.storage.migrations_v2 import registration_status

        check = migration_hold_check(registration_status())
        assert check.status == WARN
        assert "0014" in check.detail

    def test_plain_http_off_loopback_is_a_failure_not_a_note(self) -> None:
        check = provider_connection_check(
            describe={"label": "Remote Ollama", "tls": False, "loopback": False}, catalog=None
        )
        assert check.status == FAIL
        assert "cleartext" in check.detail

    def test_loopback_plain_http_is_fine(self) -> None:
        check = provider_connection_check(
            describe={"label": "Ollama", "tls": False, "loopback": True},
            catalog=CatalogResult(source="ollama", ok=True, entries=[]),
        )
        assert check.status == OK

    def test_an_unreadable_catalog_fails_the_provider_check(self) -> None:
        check = provider_connection_check(
            describe={"label": "DeepSeek", "tls": True, "loopback": False},
            catalog=CatalogResult(
                source="openai-models",
                ok=False,
                error_type="unauthorized",
                error_message="bad key",
            ),
        )
        assert check.status == FAIL

    def test_a_manual_only_provider_is_healthy(self) -> None:
        check = provider_connection_check(
            describe={"label": "Qwen", "tls": True, "loopback": False},
            catalog=CatalogResult(source="curated", ok=True, manual_only=True),
        )
        assert check.status == OK

    def test_capability_evidence_reports_source_and_age(self) -> None:
        ledger = ledger_with(
            **{
                Capability.TOOL_CALLING: (
                    CapabilityStatus.SUPPORTED,
                    EvidenceSource.LIVE_PROBE,
                )
            }
        )
        check = capability_evidence_check(model_id="m1", ledger=ledger)
        entry = check.data["capabilities"]["tool_calling"]
        assert entry["source"] == "live_probe"
        assert entry["status"] == "supported"
        assert check.status == OK

    def test_aged_probe_evidence_warns_and_is_never_refreshed_silently(self) -> None:
        """Re-probing spends the user's quota; doing it as a side effect of a diagnostic is not ours."""

        old = utcnow() - timedelta(days=90)
        ledger = CapabilityLedger().record(
            CapabilityEvidence(
                capability=Capability.TOOL_CALLING,
                status=CapabilityStatus.SUPPORTED,
                source=EvidenceSource.LIVE_PROBE,
                observed_at=old,
            )
        )
        check = capability_evidence_check(model_id="m1", ledger=ledger)
        assert check.status == WARN
        assert "tool_calling" in check.data["stale"]

    def test_an_aged_catalog_claim_is_not_called_stale(self) -> None:
        """A catalog claim does not go stale by sitting there; only a probe's observation does."""

        old = utcnow() - timedelta(days=90)
        ledger = CapabilityLedger().record(
            CapabilityEvidence(
                capability=Capability.TOOL_CALLING,
                status=CapabilityStatus.SUPPORTED,
                source=EvidenceSource.PROVIDER_CATALOG,
                observed_at=old,
            )
        )
        assert capability_evidence_check(model_id="m1", ledger=ledger).data["stale"] == []

    def test_a_model_with_no_evidence_warns(self) -> None:
        check = capability_evidence_check(model_id="m1", ledger=CapabilityLedger())
        assert check.status == WARN
        assert "unverified" in check.detail


# =========================================================================== wizard steps


class TestWizardSteps:
    def test_the_twelve_steps_exist_in_order(self) -> None:
        assert [step.value for step in WizardStep][:3] == ["runtime", "provider", "auth"]
        assert list(WizardStep)[-1] is WizardStep.CREATE
        assert len(list(WizardStep)) == 12

    def test_before_a_runtime_is_chosen_only_the_first_step_applies(self) -> None:
        assert steps_for(None) == (WizardStep.RUNTIME,)

    def test_a_cli_agent_skips_the_api_only_steps(self) -> None:
        """A CLI agent has no region or model catalog of its own; showing empty steps is noise."""

        steps = steps_for("cli")
        assert WizardStep.REGION not in steps
        assert WizardStep.MODEL_DISCOVERY not in steps
        assert WizardStep.SANDBOX in steps

    def test_an_api_agent_skips_the_sandbox_step(self) -> None:
        """There is no subprocess to sandbox."""

        steps = steps_for("api")
        assert WizardStep.SANDBOX not in steps
        assert WizardStep.REGION in steps


# =========================================================================== badges


class TestBadges:
    def test_every_badge_is_shown_including_the_unknown_ones(self) -> None:
        """A missing badge is indistinguishable from an unsupported one at a glance."""

        badges = build_badges(CapabilityLedger())
        assert len(badges) == 14
        assert all(badge.unknown for badge in badges)

    def test_a_probe_backed_badge_is_verified(self) -> None:
        ledger = ledger_with(
            **{Capability.TOOL_CALLING: (CapabilityStatus.SUPPORTED, EvidenceSource.LIVE_PROBE)}
        )
        badge = next(b for b in build_badges(ledger) if b.capability is Capability.TOOL_CALLING)
        assert badge.lit is True
        assert badge.verified is True
        assert "verified by a live probe" in badge.tooltip()

    def test_a_catalog_backed_badge_is_lit_but_not_verified(self) -> None:
        """The distinction a user choosing a tool-using model actually needs."""

        ledger = ledger_with(
            **{
                Capability.TOOL_CALLING: (
                    CapabilityStatus.SUPPORTED,
                    EvidenceSource.PROVIDER_CATALOG,
                )
            }
        )
        badge = next(b for b in build_badges(ledger) if b.capability is Capability.TOOL_CALLING)
        assert badge.lit is True
        assert badge.verified is False
        assert "not verified here" in badge.tooltip()

    def test_a_curated_preset_badge_says_it_is_the_weakest_source(self) -> None:
        ledger = ledger_with(
            **{Capability.TEXT: (CapabilityStatus.SUPPORTED, EvidenceSource.CURATED_PRESET)}
        )
        badge = next(b for b in build_badges(ledger) if b.capability is Capability.TEXT)
        assert "weakest source" in badge.tooltip()

    def test_an_aged_probe_badge_stops_counting_as_verified(self) -> None:
        old = utcnow() - (STALE_AFTER + timedelta(days=1))
        ledger = CapabilityLedger().record(
            CapabilityEvidence(
                capability=Capability.TOOL_CALLING,
                status=CapabilityStatus.SUPPORTED,
                source=EvidenceSource.LIVE_PROBE,
                observed_at=old,
            )
        )
        badge = next(b for b in build_badges(ledger) if b.capability is Capability.TOOL_CALLING)
        assert badge.stale is True
        assert badge.verified is False
        assert "aged" in badge.tooltip()

    def test_an_unknown_badge_tells_the_user_how_to_settle_it(self) -> None:
        badge = build_badges(CapabilityLedger())[0]
        assert "probe" in badge.tooltip()

    def test_a_manual_override_is_shown_as_the_users_own_statement(self) -> None:
        ledger = ledger_with(
            **{Capability.REASONING: (CapabilityStatus.SUPPORTED, EvidenceSource.MANUAL_OVERRIDE)}
        )
        badge = next(b for b in build_badges(ledger) if b.capability is Capability.REASONING)
        assert "set manually by you" in badge.tooltip()


# =========================================================================== catalog fallback


class TestCatalogFallback:
    def test_a_healthy_catalog_offers_no_fallbacks(self) -> None:
        result = CatalogResult(source="s", ok=True, entries=[CatalogEntry(model=_model("a"))])
        presented = present_catalog(result)
        assert presented.fallbacks == ()
        assert presented.authoritative is True
        assert presented.blocked is False

    def test_an_unreadable_catalog_offers_all_four_ways_forward(self) -> None:
        result = CatalogResult(
            source="s", ok=False, error_type="timeout", error_message="timed out"
        )
        presented = present_catalog(result, cached_available=True)
        assert set(presented.fallbacks) == {
            CatalogFallback.RETRY,
            CatalogFallback.USE_CACHED,
            CatalogFallback.MANUAL_ID,
            CatalogFallback.PROVIDER_DEFAULT,
        }
        assert presented.blocked is True
        assert "not the same as the provider having no models" in presented.message

    def test_the_cached_option_is_only_offered_when_a_cache_exists(self) -> None:
        result = CatalogResult(source="s", ok=False, error_type="timeout", error_message="x")
        presented = present_catalog(result, cached_available=False)
        assert CatalogFallback.USE_CACHED not in presented.fallbacks

    def test_a_partial_catalog_shows_what_it_has_and_says_it_is_incomplete(self) -> None:
        result = CatalogResult(
            source="s", ok=False, partial=True, entries=[CatalogEntry(model=_model("a"))]
        )
        presented = present_catalog(result)
        assert len(presented.models) == 1
        assert presented.authoritative is False
        assert presented.blocked is False
        assert CatalogFallback.RETRY in presented.fallbacks

    def test_manual_only_is_presented_as_a_configuration_not_a_failure(self) -> None:
        result = CatalogResult(source="curated", ok=True, manual_only=True, entries=[])
        presented = present_catalog(result)
        assert CatalogFallback.MANUAL_ID in presented.fallbacks
        assert "no listable catalog" in presented.message
        assert "fail" not in presented.message.lower()


def _model(model_id: str):
    from openagent.core.models import RemoteModel

    return RemoteModel(id=model_id, display_name=model_id)


# =========================================================================== probe gate


class TestProbeGate:
    def test_no_requirement_means_no_gate(self) -> None:
        gate = evaluate_probe_gate(CapabilityLedger(), RequiredCapabilities())
        assert gate.satisfied is True
        assert gate.requires_override is False

    def test_a_verified_requirement_passes(self) -> None:
        ledger = ledger_with(
            **{Capability.TOOL_CALLING: (CapabilityStatus.SUPPORTED, EvidenceSource.LIVE_PROBE)}
        )
        gate = evaluate_probe_gate(ledger, RequiredCapabilities(tools=True))
        assert gate.satisfied is True

    def test_a_known_unsupported_requirement_needs_an_override(self) -> None:
        ledger = ledger_with(
            **{Capability.TOOL_CALLING: (CapabilityStatus.UNSUPPORTED, EvidenceSource.LIVE_PROBE)}
        )
        gate = evaluate_probe_gate(ledger, RequiredCapabilities(tools=True))
        assert gate.requires_override is True
        assert gate.unsupported == (Capability.TOOL_CALLING,)
        assert "will fail at the first attempt" in gate.message()

    def test_an_unverified_requirement_also_needs_an_override_with_a_different_message(
        self,
    ) -> None:
        """ "Cannot do it" and "nobody checked" lead a user to different decisions."""

        gate = evaluate_probe_gate(CapabilityLedger(), RequiredCapabilities(tools=True))
        assert gate.requires_override is True
        assert gate.unverified == (Capability.TOOL_CALLING,)
        assert "nothing here has seen it work" in gate.message()
        assert "does not support" not in gate.message()

    def test_a_blocked_probe_says_so_rather_than_blaming_the_model(self) -> None:
        gate = evaluate_probe_gate(
            CapabilityLedger(),
            RequiredCapabilities(tools=True),
            probe_blocked_reason="the credential was rejected",
        )
        assert "could not run" in gate.message()
        assert "credential was rejected" in gate.message()

    def test_several_requirements_are_all_reported(self) -> None:
        gate = evaluate_probe_gate(
            CapabilityLedger(), RequiredCapabilities(tools=True, reasoning=True, vision=True)
        )
        assert len(gate.unverified) == 3


# =========================================================================== disclosures


class TestDisclosures:
    @pytest.mark.parametrize("provider", ["gemini", "qwen", "lmstudio"])
    def test_every_server_state_provider_has_a_disclosure(self, provider: str) -> None:
        disclosure = server_state_disclosure(provider)
        assert disclosure is not None
        assert disclosure.body

    def test_the_wording_is_concrete_about_who_retains_the_conversation(self) -> None:
        """Abstract phrasing is what lets someone enable this without registering what it means."""

        disclosure = server_state_disclosure("gemini")
        assert disclosure is not None
        assert "Google" in disclosure.title

    def test_a_provider_without_server_state_has_no_disclosure(self) -> None:
        assert server_state_disclosure("deepseek") is None

    def test_no_disclosure_when_server_state_is_off(self) -> None:
        assert required_disclosures(provider_type="gemini", server_state_enabled=False) == []

    def test_insecure_http_is_disclosed_regardless_of_provider(self) -> None:
        disclosures = required_disclosures(
            provider_type="deepseek", server_state_enabled=False, insecure_http=True
        )
        assert len(disclosures) == 1
        assert "cleartext" in disclosures[0].body


# =========================================================================== review


class TestReview:
    def test_review_separates_verified_from_merely_claimed(self) -> None:
        ledger = ledger_with(
            **{
                Capability.TOOL_CALLING: (
                    CapabilityStatus.SUPPORTED,
                    EvidenceSource.LIVE_PROBE,
                ),
                Capability.REASONING: (
                    CapabilityStatus.SUPPORTED,
                    EvidenceSource.PROVIDER_CATALOG,
                ),
            }
        )
        review = build_review(
            runtime="api", badges=build_badges(ledger), provider="deepseek", model="m1"
        )
        assert "Tools" in review.verified_capabilities
        assert "Reasoning" in review.unverified_capabilities
        assert "Reasoning" not in review.verified_capabilities

    def test_the_review_projection_has_no_field_a_secret_could_occupy(self) -> None:
        review = build_review(
            runtime="api",
            badges=[],
            provider="deepseek",
            protocol=Protocol.OPENAI_CHAT,
            credential_source="keychain",
        )
        payload = review.to_dict()
        assert payload["credential_source"] == "keychain"
        assert not [key for key in payload if "key" in key or "secret" in key or "token" in key]

    def test_review_carries_the_disclosures_the_configuration_owes(self) -> None:
        review = build_review(
            runtime="api", badges=[], provider="gemini", server_state_enabled=True
        )
        assert [d.key for d in review.disclosures] == ["server-state"]

    def test_an_override_reason_is_recorded_in_the_review(self) -> None:
        review = build_review(
            runtime="api",
            badges=[],
            provider="deepseek",
            probe_override_reason="I know this model works",
        )
        assert review.to_dict()["probe_override_reason"] == "I know this model works"
