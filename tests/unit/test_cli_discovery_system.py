"""The unified CLI discovery flow (spec §19).

Two things are worth testing here above everything else.

**The status vocabulary must stay discriminating.** Six labels exist because there are six genuinely
different situations, and the value of the middle four is entirely in *not* being "installed". A change
that collapses two of them would pass every other test in the suite.

**A probe of one CLI must never receive another provider's credential.** This is the only failure in
the module with consequences outside the process, and it is silent: a Gemini probe that inherits
``ANTHROPIC_API_KEY`` works perfectly and sends someone's Anthropic key to a Google binary.
"""

from __future__ import annotations

import json

import pytest

from openagent.core.models import CliInstallSource
from openagent.runtimes.cli.discovery import (
    DESCRIPTORS,
    CliDiscoveryReport,
    DiscoveryStatus,
    OutputProtocol,
    ResumeStrategy,
    _classify,
    discover_all,
    discover_cli,
    discovery_environment,
    get_descriptor,
    scratch_directory,
)
from openagent.runtimes.cli.registry import EXPERIMENTAL, FIRST_CLASS, known_cli_types

CLIS = ("codex", "claude", "gemini", "antigravity", "qwen", "kimi")


class TestDescriptors:
    @pytest.mark.parametrize("cli_type", CLIS)
    def test_every_registered_cli_has_a_descriptor(self, cli_type: str) -> None:
        assert get_descriptor(cli_type) is not None

    def test_the_descriptor_table_matches_the_registry(self) -> None:
        """A registered CLI with no descriptor discovers as an unknown; the reverse is dead data."""

        assert set(DESCRIPTORS) == set(known_cli_types())

    @pytest.mark.parametrize("cli_type", CLIS)
    def test_a_descriptor_cannot_make_a_capability_claim(self, cli_type: str) -> None:
        """It has no field meaning "supported" — only which mechanism to look for (spec §19)."""

        descriptor = get_descriptor(cli_type)
        assert descriptor is not None
        fields = set(type(descriptor).__dataclass_fields__)
        assert not {f for f in fields if f.startswith("supports_")}
        assert isinstance(descriptor.resume_strategy, ResumeStrategy)
        assert isinstance(descriptor.output_protocol, OutputProtocol)

    @pytest.mark.parametrize("cli_type", CLIS)
    def test_a_descriptor_names_its_credential_variables(self, cli_type: str) -> None:
        descriptor = get_descriptor(cli_type)
        assert descriptor is not None
        assert descriptor.auth_environment, f"{cli_type} documents no credential variable"

    @pytest.mark.parametrize("cli_type", CLIS)
    def test_install_methods_are_declared(self, cli_type: str) -> None:
        """The updater refuses a provenance not in this list, so an empty list disables updates."""

        descriptor = get_descriptor(cli_type)
        assert descriptor is not None
        assert descriptor.install_methods
        assert all(isinstance(method, CliInstallSource) for method in descriptor.install_methods)

    def test_only_gemini_reports_a_non_streaming_protocol(self) -> None:
        """Headless gemini returns one document at the end; that is why it is the exception."""

        single = {
            cli for cli, d in DESCRIPTORS.items() if d.output_protocol is OutputProtocol.SINGLE_JSON
        }
        assert single == {"gemini"}

    def test_gemini_declares_resume_unsupported(self) -> None:
        assert DESCRIPTORS["gemini"].resume_strategy is ResumeStrategy.UNSUPPORTED

    def test_kimi_declares_a_protocol_load_resume(self) -> None:
        assert DESCRIPTORS["kimi"].resume_strategy is ResumeStrategy.PROTOCOL_LOAD


class TestCredentialIsolation:
    """The failure with consequences outside the process (spec §19.2)."""

    def test_a_probe_receives_only_its_own_cli_credentials(self, monkeypatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
        monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
        monkeypatch.setenv("MOONSHOT_API_KEY", "moonshot-secret")

        env = discovery_environment(DESCRIPTORS["gemini"])
        assert env.get("GEMINI_API_KEY") == "gemini-secret"
        assert "ANTHROPIC_API_KEY" not in env
        assert "OPENAI_API_KEY" not in env
        assert "MOONSHOT_API_KEY" not in env

    def test_a_claude_probe_does_not_receive_a_gemini_key(self, monkeypatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
        env = discovery_environment(DESCRIPTORS["claude"])
        assert env.get("ANTHROPIC_API_KEY") == "anthropic-secret"
        assert "GEMINI_API_KEY" not in env

    def test_a_kimi_probe_does_not_receive_a_dashscope_key(self, monkeypatch) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-secret")
        monkeypatch.setenv("MOONSHOT_API_KEY", "moonshot-secret")
        env = discovery_environment(DESCRIPTORS["kimi"])
        assert "DASHSCOPE_API_KEY" not in env

    def test_qwen_legitimately_receives_an_openai_key_because_it_documents_one(
        self, monkeypatch
    ) -> None:
        """Qwen Code accepts an OpenAI-compatible endpoint, so the variable is *its* variable here.

        The rule is "only what this CLI documents", not "never an OpenAI key" — the second would break
        a documented configuration.
        """

        monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
        env = discovery_environment(DESCRIPTORS["qwen"])
        assert env.get("OPENAI_API_KEY") == "openai-secret"
        assert "ANTHROPIC_API_KEY" not in env

    def test_no_descriptor_means_no_credential_at_all(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
        env = discovery_environment(None)
        assert not [name for name in env if name.endswith("_API_KEY")]

    def test_probing_happens_from_an_empty_directory(self) -> None:
        """A CLI run inside a project reads its hooks and extensions; discovery must not trigger that."""

        directory = scratch_directory()
        try:
            assert directory.is_dir()
            assert list(directory.iterdir()) == []
        finally:
            import shutil

            shutil.rmtree(directory, ignore_errors=True)


class TestStatusClassification:
    def _report(self, **kwargs) -> CliDiscoveryReport:
        base = {
            "cli_type": "codex",
            "display_name": "Codex CLI",
            "descriptor": DESCRIPTORS["codex"],
            "status": DiscoveryStatus.NOT_INSTALLED,
            "status_label": "",
            "installed": True,
            "authenticated": True,
            "version": "codex-cli 0.142.5",
            "validated_version": "codex-cli 0.142.5",
            "version_verified": True,
        }
        base.update(kwargs)
        return CliDiscoveryReport(**base)  # type: ignore[arg-type]

    def test_not_installed(self) -> None:
        status, label = _classify(self._report(installed=False), "codex")
        assert status is DiscoveryStatus.NOT_INSTALLED
        assert "not installed" in label

    def test_a_blocking_auth_failure_makes_it_unusable(self) -> None:
        """Claude Code installed but signed out: selecting it would produce a run that cannot work."""

        status, label = _classify(
            self._report(auth_blocking=True, authenticated=False, auth_detail="not signed in"),
            "claude",
        )
        assert status is DiscoveryStatus.UNSUPPORTED
        assert status.usable is False
        assert "not signed in" in label

    def test_a_version_mismatch_is_reported_specifically(self) -> None:
        status, label = _classify(
            self._report(version="codex-cli 0.145.0", version_verified=False), "codex"
        )
        assert status is DiscoveryStatus.VERSION_UNVERIFIED
        assert "0.145.0" in label and "0.142.5" in label
        assert status.usable is True

    def test_an_undeterminable_auth_state_is_its_own_answer(self) -> None:
        """ "We could not tell" is different from "not authenticated" and more useful."""

        status, _ = _classify(self._report(authenticated=None), "codex")
        assert status is DiscoveryStatus.AUTH_UNKNOWN
        assert status.usable is True

    def test_an_experimental_cli_says_so_even_when_everything_else_is_fine(self) -> None:
        status, label = _classify(
            self._report(cli_type="qwen", descriptor=DESCRIPTORS["qwen"], validated_version=None),
            "qwen",
        )
        assert status is DiscoveryStatus.EXPERIMENTAL
        assert "experimental" in label.lower()

    def test_a_first_class_verified_cli_is_verified_live(self) -> None:
        status, _ = _classify(self._report(), "codex")
        assert status is DiscoveryStatus.VERIFIED_LIVE

    def test_the_six_statuses_stay_distinct(self) -> None:
        """The value of the middle four is entirely in not being "installed"."""

        assert len({status.value for status in DiscoveryStatus}) == 7
        usable = {status for status in DiscoveryStatus if status.usable}
        assert DiscoveryStatus.NOT_INSTALLED not in usable
        assert DiscoveryStatus.UNSUPPORTED not in usable

    def test_a_blocking_auth_failure_outranks_a_version_mismatch(self) -> None:
        """Most-specific-first: the reason the user needs is the one that stops them."""

        status, _ = _classify(
            self._report(auth_blocking=True, version_verified=False, authenticated=False), "codex"
        )
        assert status is DiscoveryStatus.UNSUPPORTED


class TestLiveStreamingDerivation:
    def test_a_streaming_protocol_implies_live_events(self) -> None:
        assert DESCRIPTORS["codex"].output_protocol is OutputProtocol.STREAM_JSON

    def test_a_single_document_protocol_does_not(self) -> None:
        assert DESCRIPTORS["gemini"].output_protocol is OutputProtocol.SINGLE_JSON


class TestDiscoveryFlow:
    async def test_an_absent_cli_produces_a_complete_row_not_an_absent_one(self) -> None:
        """An absent row reads as "we did not look", which is what this module exists to avoid."""

        report = await discover_cli("gemini")
        assert report.cli_type == "gemini"
        assert report.installed in (True, False)
        assert report.status_label
        assert report.descriptor is not None

    async def test_an_unknown_cli_is_reported_rather_than_raising(self) -> None:
        report = await discover_cli("not-a-real-cli")
        assert report.installed is False
        assert report.warnings

    async def test_every_registered_cli_gets_a_row(self) -> None:
        reports = await discover_all(include_models=False)
        assert {report.cli_type for report in reports} == set(known_cli_types())

    async def test_the_json_projection_is_serializable_and_secret_free(self, monkeypatch) -> None:
        secret = "sk-live-must-not-appear-in-doctor-output-0123456789"
        for name in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "MOONSHOT_API_KEY"):
            monkeypatch.setenv(name, secret)
        reports = await discover_all(include_models=False)
        rendered = json.dumps([report.to_dict() for report in reports])
        assert secret not in rendered

    async def test_updates_are_not_checked_unless_asked(self) -> None:
        """Doctor must be runnable offline, so an update check is opt-in."""

        report = await discover_cli("gemini", include_models=False)
        assert report.update_state == "unknown"

    async def test_a_report_names_its_permission_profiles(self) -> None:
        from openagent.core.permissions import PROFILES

        report = await discover_cli("codex", include_models=False)
        if report.installed:
            assert set(report.permission_profiles) == set(PROFILES)


class TestEvidenceBuckets:
    def test_first_class_and_experimental_cover_every_cli(self) -> None:
        assert set(FIRST_CLASS) | set(EXPERIMENTAL) == set(known_cli_types())

    def test_experimental_descriptors_are_flagged_as_such(self) -> None:
        for cli_type in EXPERIMENTAL:
            descriptor = get_descriptor(cli_type)
            assert descriptor is not None
            assert descriptor.experimental is True

    def test_a_first_class_cli_is_not_flagged_experimental_in_its_descriptor(self) -> None:
        for cli_type in FIRST_CLASS:
            descriptor = get_descriptor(cli_type)
            assert descriptor is not None
            if cli_type == "gemini":
                # Gemini is first-class by intent (spec §19.4) and experimental by evidence: its
                # mapping is fixture-validated. Both facts are recorded rather than reconciled away.
                continue
            assert descriptor.experimental is False
