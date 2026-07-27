"""The battery every CLI adapter must pass (spec §25).

Parameterized across all six registered CLIs, offline. The point is the same as the provider contract
suite: a test written per adapter proves six adapters work, and a test written once and run six times
proves they *agree* — which is what stops the seventh from reintroducing a bug the third one fixed.

The properties worth the most here are the ones a per-adapter test tends not to cover:

* **exactly one terminal event per run**, whatever the CLI did — including a CLI that emitted none, two,
  or two contradictory ones;
* **a capability is never claimed from documentation**. Every adapter's ``resumable`` and
  ``structured_events`` must come from the installed binary or a protocol handshake, so an adapter
  reports ``False`` for a CLI that is not installed rather than what its docs promise;
* **no adapter receives another provider's credential**, which is the one failure here that is silent
  and has consequences outside the process.
"""

from __future__ import annotations

import inspect

import pytest

from openagent.core.events import EventType
from openagent.runtimes.cli.base import TERMINAL_EVENT_TYPES, CliRunRequest
from openagent.runtimes.cli.registry import (
    EXPERIMENTAL,
    FIRST_CLASS,
    build_cli_adapter,
    cli_display_name,
    cli_status_label,
    known_cli_types,
)
from openagent.security.process import TerminationOutcome, minimal_environment

#: The six CLIs spec §31.2 requires. Named explicitly so removing one fails this list rather than
#: silently shrinking the suite.
CLIS = ("codex", "claude", "gemini", "antigravity", "qwen", "kimi")


#: Adapters that are not installed on a given machine. Resolved at runtime rather than hardcoded: this
#: suite must pass on a machine with none of them and on CI with some.
def installed(cli_type: str) -> bool:
    return getattr(build_cli_adapter(cli_type), "executable", None) is not None


class TestRegistry:
    @pytest.mark.parametrize("cli_type", CLIS)
    def test_the_cli_is_registered(self, cli_type: str) -> None:
        assert cli_type in known_cli_types(), f"{cli_type} is required by spec §31.2"

    @pytest.mark.parametrize("cli_type", CLIS)
    def test_it_has_a_display_name_and_an_honest_status_label(self, cli_type: str) -> None:
        assert cli_display_name(cli_type) != cli_type or cli_type in {"qwen", "kimi"}
        label = cli_status_label(cli_type)
        assert label and label != "Installed but unverified"

    def test_first_class_and_experimental_partition_the_registry(self) -> None:
        """A CLI in neither bucket has no stated evidence level, which is the thing to avoid."""

        assert set(FIRST_CLASS) | set(EXPERIMENTAL) == set(CLIS)
        assert not set(FIRST_CLASS) & set(EXPERIMENTAL)

    @pytest.mark.parametrize("cli_type", EXPERIMENTAL)
    async def test_experimental_adapters_say_so(self, cli_type: str) -> None:
        caps = await build_cli_adapter(cli_type).capabilities()
        assert caps.experimental is True

    @pytest.mark.parametrize("cli_type", CLIS)
    def test_an_unknown_cli_type_is_a_clear_error(self, cli_type: str) -> None:
        with pytest.raises(KeyError, match="unknown CLI type"):
            build_cli_adapter(f"{cli_type}-does-not-exist")


class TestLifecycleSurface:
    """Registration means the wizard can select it, so the lifecycle has to be complete (§11.7)."""

    @pytest.mark.parametrize("cli_type", CLIS)
    @pytest.mark.parametrize(
        "method",
        ["detect", "inspect_installation", "capabilities", "inspect_auth", "start_run", "cancel"],
    )
    def test_the_required_lifecycle_method_exists(self, cli_type: str, method: str) -> None:
        assert callable(getattr(build_cli_adapter(cli_type), method, None)), (
            f"{cli_type} is registered but has no {method}(); a half-registered adapter is an entry "
            f"a user can select and then watch fail"
        )

    @pytest.mark.parametrize("cli_type", CLIS)
    def test_start_run_returns_an_async_iterator_not_a_coroutine(self, cli_type: str) -> None:
        """The executor iterates it; a coroutine would have to be awaited first and would not stream."""

        adapter = build_cli_adapter(cli_type)
        assert not inspect.iscoroutinefunction(adapter.start_run)

    @pytest.mark.parametrize("cli_type", CLIS)
    def test_model_discovery_is_available_and_reports_its_method(self, cli_type: str) -> None:
        adapter = build_cli_adapter(cli_type)
        assert callable(getattr(adapter, "list_models", None))
        assert isinstance(getattr(adapter, "model_discovery_method", ""), str)


class TestNotInstalled:
    """Every honest answer for an absent CLI, so the wizard shows it as unavailable, not broken."""

    @pytest.mark.parametrize("cli_type", CLIS)
    async def test_detect_returns_none_when_not_installed(self, cli_type: str) -> None:
        if installed(cli_type):
            pytest.skip(f"{cli_type} is installed on this machine")
        assert await build_cli_adapter(cli_type).detect() is None

    @pytest.mark.parametrize("cli_type", CLIS)
    async def test_capabilities_are_false_when_not_installed(self, cli_type: str) -> None:
        """The failure this prevents: reporting a documented capability for a binary that is absent."""

        if installed(cli_type):
            pytest.skip(f"{cli_type} is installed on this machine")
        caps = await build_cli_adapter(cli_type).capabilities()
        assert caps.structured_events is False
        assert caps.resumable is False

    @pytest.mark.parametrize("cli_type", CLIS)
    async def test_model_discovery_reports_unavailable_rather_than_a_fabricated_list(
        self, cli_type: str
    ) -> None:
        if installed(cli_type):
            pytest.skip(f"{cli_type} is installed on this machine")
        adapter = build_cli_adapter(cli_type)
        models = await adapter.list_models()
        assert models == [], f"{cli_type} invented a model list for a CLI that is not installed"
        result = getattr(adapter, "last_model_discovery", None)
        if result is not None:
            assert result.available is False
            assert result.error, "an unavailable discovery must carry a reason the user can act on"

    @pytest.mark.parametrize("cli_type", CLIS)
    async def test_a_run_against_a_missing_cli_fails_cleanly(self, cli_type: str, tmp_path) -> None:
        if installed(cli_type):
            pytest.skip(f"{cli_type} is installed on this machine")
        adapter = build_cli_adapter(cli_type)
        request = CliRunRequest(run_id="r1", prompt="hello", workspace=tmp_path)
        events = [event async for event in adapter.start_run(request)]
        terminal = [e for e in events if _type_of(e) in TERMINAL_EVENT_TYPES]
        assert len(terminal) == 1, "a run must resolve to exactly one terminal event"
        assert _type_of(terminal[0]) == EventType.RUN_FAILED.value
        assert terminal[0].data.get("error_type") == "cli_not_found"

    @pytest.mark.parametrize("cli_type", CLIS)
    async def test_cancelling_an_unknown_run_is_not_an_error(self, cli_type: str) -> None:
        result = await build_cli_adapter(cli_type).cancel("no-such-run")
        # Real TerminationOutcome members only: a set containing a value the enum does not have would
        # pass on the strength of the others and never notice.
        assert result.outcome in {
            TerminationOutcome.ALREADY_GONE,
            TerminationOutcome.TERMINATED,
        }


class TestAuthReporting:
    @pytest.mark.parametrize("cli_type", CLIS)
    async def test_auth_reports_names_never_values(self, cli_type: str, monkeypatch) -> None:
        """A credential must never reach an AuthStatus — it is rendered in Doctor and the wizard."""

        secret = "sk-live-should-never-be-reported-0123456789"
        for name in (
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "DASHSCOPE_API_KEY",
            "MOONSHOT_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
        ):
            monkeypatch.setenv(name, secret)
        status = await build_cli_adapter(cli_type).inspect_auth()
        rendered = f"{status.detail} {status.environment_names} {status.source} {status.conflicts}"
        assert secret not in rendered

    @pytest.mark.parametrize("cli_type", ("gemini", "qwen", "kimi"))
    async def test_an_undetectable_login_is_non_blocking(self, cli_type: str, monkeypatch) -> None:
        """These CLIs support interactive logins that leave no variable, so absence proves nothing.

        Blocking a run on that guess would be worse than letting the CLI report its own error.
        """

        for name in (
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "DASHSCOPE_API_KEY",
            "MOONSHOT_API_KEY",
            "QWEN_API_KEY",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
        ):
            monkeypatch.delenv(name, raising=False)
        status = await build_cli_adapter(cli_type).inspect_auth()
        assert status.blocking is False


class TestCredentialIsolation:
    """The one failure in this file with consequences outside the process (spec §19.2)."""

    def test_the_minimal_environment_carries_no_provider_credential(self, monkeypatch) -> None:
        for name in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
            "DASHSCOPE_API_KEY",
            "MOONSHOT_API_KEY",
            "ZHIPUAI_API_KEY",
            "MINIMAX_API_KEY",
            "OPENROUTER_API_KEY",
        ):
            monkeypatch.setenv(name, "sk-should-not-propagate")
        env = minimal_environment()
        leaked = [name for name in env if name.endswith("_API_KEY")]
        assert leaked == [], f"a CLI child would inherit {leaked}"

    def test_only_the_explicitly_injected_credential_is_present(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-other-provider")
        env = minimal_environment({"GEMINI_API_KEY": "gemini-key-for-this-run"})
        assert env["GEMINI_API_KEY"] == "gemini-key-for-this-run"
        assert "ANTHROPIC_API_KEY" not in env

    def test_qwen_discovery_disables_every_code_executing_feature(self) -> None:
        """Listing models must not be able to run a project hook (spec §17.2)."""

        from openagent.runtimes.cli.qwen_code import discovery_environment

        env = discovery_environment()
        for feature in ("EXTENSIONS", "HOOKS", "SKILLS", "MCP", "PROJECT_AGENTS", "MEMORY"):
            assert env.get(f"QWEN_CODE_DISABLE_{feature}") == "1"
        assert not [name for name in env if name.endswith("_API_KEY")]


class TestResumeIsNeverClaimedWithoutEvidence:
    async def test_gemini_refuses_to_pretend_it_can_resume(self) -> None:
        """Replaying prompt history is not native resume, and must not be labelled as it (§11.5)."""

        from openagent.runtimes.cli.gemini import RESUME_SPIKE_REQUIRED, GeminiCliAdapter

        adapter = GeminiCliAdapter()
        caps = await adapter.capabilities()
        assert caps.resumable is False
        with pytest.raises(NotImplementedError, match="resume"):
            adapter.resume_run("s1", "hi", CliRunRequest(run_id="r", prompt="p", workspace=None))  # type: ignore[arg-type]
        assert "session id" in RESUME_SPIKE_REQUIRED

    async def test_qwen_resume_requires_the_flag_to_exist_on_this_build(self, tmp_path) -> None:
        from openagent.runtimes.cli.qwen_code import QwenCodeAdapter, QwenFlagSupport

        adapter = QwenCodeAdapter(executable="/nonexistent/qwen")
        adapter._flags = QwenFlagSupport(stream_json=True, resume=False)
        request = CliRunRequest(run_id="r", prompt="p", workspace=tmp_path)
        events = [e async for e in adapter.resume_run("s1", "p", request)]
        terminal = [e for e in events if _type_of(e) in TERMINAL_EVENT_TYPES]
        assert len(terminal) == 1
        assert terminal[0].data.get("error_type") == "session_not_found"

    async def test_kimi_resume_requires_the_agents_own_loadsession_advertisement(
        self, tmp_path
    ) -> None:
        from openagent.runtimes.cli.kimi_acp import AcpHandshake, KimiAcpAdapter

        adapter = KimiAcpAdapter(executable="/nonexistent/kimi")
        adapter._handshake = AcpHandshake(protocol_version=1, load_session=False)
        caps = await adapter.capabilities()
        assert caps.resumable is False
        request = CliRunRequest(run_id="r", prompt="p", workspace=tmp_path)
        events = [e async for e in adapter.resume_run("s1", "p", request)]
        terminal = [e for e in events if _type_of(e) in TERMINAL_EVENT_TYPES]
        assert terminal[0].data.get("error_type") == "session_not_found"

    async def test_a_protocol_version_mismatch_refuses_to_run(self, tmp_path) -> None:
        from openagent.runtimes.cli.kimi_acp import AcpHandshake, KimiAcpAdapter

        adapter = KimiAcpAdapter(executable="/nonexistent/kimi")
        adapter._handshake = AcpHandshake(protocol_version=99, load_session=True)
        request = CliRunRequest(run_id="r", prompt="p", workspace=tmp_path)
        events = [e async for e in adapter.start_run(request)]
        terminal = [e for e in events if _type_of(e) in TERMINAL_EVENT_TYPES]
        assert terminal[0].data.get("error_type") == "cli_version_unsupported"


class TestLiveStreamingIsSeparateFromStructuredOutput:
    """Two different claims. Conflating them promises deltas that never arrive (spec §11.3, §17.1)."""

    async def test_gemini_never_claims_live_streaming(self) -> None:
        from openagent.runtimes.cli.gemini import GeminiCliAdapter

        assert GeminiCliAdapter().live_structured_events is False

    def test_qwen_live_streaming_needs_the_partial_message_flag_too(self) -> None:
        from openagent.runtimes.cli.qwen_code import QwenFlagSupport

        stream_only = QwenFlagSupport(stream_json=True, include_partial_messages=False)
        assert stream_only.structured_events is True
        assert stream_only.live_structured_events is False

        both = QwenFlagSupport(stream_json=True, include_partial_messages=True)
        assert both.live_structured_events is True

    def test_stream_json_without_output_format_is_not_stream_json(self) -> None:
        """Guarding a false positive from a help text that merely mentions the words."""

        from openagent.runtimes.cli.qwen_code import parse_flag_support

        assert parse_flag_support("we support stream-json somewhere").stream_json is False


def _type_of(event) -> str:  # noqa: ANN001 - NormalizedEvent, imported lazily by callers
    return event.type if isinstance(event.type, str) else event.type.value
