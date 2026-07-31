"""The v0.2 security audit (spec §27).

Every check here is a *silent* failure — one where the system keeps working and the damage is
invisible until much later, or invisible entirely. That is the selection criterion: a crash is caught
by any test, a credential written to a log is caught by this file or by nobody.

Grouped by what leaks, in rough order of how bad it is to get wrong:

* a credential reaching somewhere durable — a log, a URL, a report, another vendor's process;
* an unbounded input turning a hostile response into memory exhaustion;
* a boundary crossed — a path escaping the workspace, a symlink followed, a plain-HTTP endpoint;
* a retry storm turning one rejection into four;
* a tool executing without having been validated, approved, and scoped.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pytest_httpx import HTTPXMock

from openagent.core.errors import ErrorType, is_retryable, redact_secrets
from openagent.core.models import Protocol
from openagent.providers.base import Message, NormalizedModelRequest, Role, collect
from openagent.providers.continuation import ContinuationEnvelope, ContinuationStrategy
from openagent.providers.spec import get_spec, requires_tls
from openagent.providers.transport import Transport, TransportError
from openagent.providers.wire_adapter import InsecureEndpointError, WireProviderAdapter
from openagent.runtimes.cli.discovery import DESCRIPTORS, discovery_environment
from openagent.security.process import minimal_environment

pytestmark = pytest.mark.security

#: A token shaped like a real credential, distinctive enough that any leak is unambiguous.
SECRET = "sk-live-51Hq8xZm4kPvR7wNbT2yE9dLcA6sJfG0"


def adapter(provider: str = "deepseek", **kwargs) -> WireProviderAdapter:
    spec = get_spec(provider)
    assert spec is not None
    _, url = spec.resolve()
    kwargs.setdefault(
        "transport",
        Transport(base_url=url.rstrip("/"), headers={}, max_retries=0, backoff_base=0.0),
    )
    return WireProviderAdapter(spec=spec, api_key=SECRET, **kwargs)


def req(**kwargs) -> NormalizedModelRequest:
    fields = {
        "model": "m",
        "messages": [Message(role=Role.USER, content="hi")],
        "stream": False,
    }
    fields.update(kwargs)
    return NormalizedModelRequest(**fields)  # type: ignore[arg-type]


# =========================================================================== credential leaks


class TestCredentialsNeverBecomeDurable:
    async def test_a_provider_error_echoing_the_key_is_redacted(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """Providers do echo the rejected Authorization header. The error must not carry it onward."""

        httpx_mock.add_response(
            status_code=401, json={"error": {"message": f"Bearer {SECRET} was rejected"}}
        )
        result = await collect(adapter().stream_response(req()))
        assert SECRET not in (result.error_message or "")

    async def test_a_transport_error_is_redacted_at_construction(self) -> None:
        """Redacted once, where the error is built — not at each of the five rendering paths."""

        error = TransportError(ErrorType.AUTHENTICATION_FAILED, f"rejected key {SECRET}")
        assert SECRET not in error.message
        assert SECRET not in str(error)

    def test_the_credential_never_enters_a_url(self) -> None:
        """A key in a URL lands in proxy logs and anything that records a request line."""

        for provider in ("gemini", "deepseek", "kimi", "qwen", "glm", "minimax", "openrouter"):
            built = adapter(provider)
            assert SECRET not in built.base_url
            assert SECRET not in built.transport.base_url

    def test_a_transport_repr_does_not_disclose_its_headers(self) -> None:
        """A dataclass repr reaches exception tracebacks and debug output."""

        transport = Transport(
            base_url="https://api.test", headers={"Authorization": f"Bearer {SECRET}"}
        )
        assert SECRET not in repr(transport)

    def test_the_doctor_projection_of_a_provider_is_secret_free(self) -> None:
        assert SECRET not in json.dumps(adapter().describe())

    def test_a_continuation_envelope_summary_carries_no_payload(self) -> None:
        envelope = ContinuationEnvelope.build(
            provider_type="deepseek",
            protocol=Protocol.OPENAI_CHAT,
            strategy=ContinuationStrategy.NATIVE_MESSAGE_REPLAY,
            native_assistant_message={"role": "assistant", "content": f"remember {SECRET}"},
        )
        redacted = json.dumps(envelope.redacted())
        assert SECRET not in redacted
        assert "content" not in redacted

    @pytest.mark.parametrize(
        "text",
        [
            f"Authorization: Bearer {SECRET}",
            f'{{"api_key": "{SECRET}"}}',
            f"x-api-key: {SECRET}",
        ],
    )
    def test_the_shared_redactor_catches_the_common_shapes(self, text: str) -> None:
        assert SECRET not in redact_secrets(text)


class TestCredentialsDoNotCrossProcesses:
    """One provider's key reaching another provider's binary (spec §19.2)."""

    def test_a_cli_child_inherits_no_provider_credential(self, monkeypatch) -> None:
        for name in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
            "DASHSCOPE_API_KEY",
            "MOONSHOT_API_KEY",
            "ZHIPUAI_API_KEY",
            "MINIMAX_API_KEY",
            "OPENROUTER_API_KEY",
            "DEEPSEEK_API_KEY",
        ):
            monkeypatch.setenv(name, SECRET)
        env = minimal_environment()
        assert SECRET not in json.dumps(env)

    @pytest.mark.parametrize(
        "cli_type", ["codex", "claude", "gemini", "antigravity", "qwen", "kimi"]
    )
    def test_a_discovery_probe_receives_only_its_own_variables(
        self, cli_type: str, monkeypatch
    ) -> None:
        descriptor = DESCRIPTORS[cli_type]
        foreign = [
            name
            for other, other_descriptor in DESCRIPTORS.items()
            if other != cli_type
            for name in other_descriptor.auth_environment
            if name not in descriptor.auth_environment
        ]
        for name in [*descriptor.auth_environment, *foreign]:
            monkeypatch.setenv(name, SECRET)

        env = discovery_environment(descriptor)
        for name in foreign:
            assert name not in env, f"a {cli_type} probe would receive {name}"

    def test_qwen_discovery_cannot_execute_project_code(self) -> None:
        """Listing models must not be able to run a project hook (spec §17.2)."""

        from openagent.runtimes.cli.qwen_code import discovery_environment as qwen_env

        env = qwen_env()
        for feature in ("EXTENSIONS", "HOOKS", "SKILLS", "MCP", "PROJECT_AGENTS", "MEMORY"):
            assert env.get(f"QWEN_CODE_DISABLE_{feature}") == "1"


# =========================================================================== unbounded input


class TestHostileResponsesAreBounded:
    async def test_a_stream_of_argument_fragments_cannot_grow_without_limit(self) -> None:
        """A provider looping on a fragment must not be able to exhaust memory."""

        from openagent.providers.streaming import (
            MAX_TOOL_ARGUMENT_BYTES,
            StreamingTurnAssembler,
        )

        assembler = StreamingTurnAssembler()
        assembler.append_tool_name("ping", index=0)
        chunk = "x" * 65536
        for _ in range((MAX_TOOL_ARGUMENT_BYTES // len(chunk)) + 10):
            assembler.append_tool_argument_fragment(chunk, index=0)
        turn = assembler.build()
        assert turn.truncated is True
        assert len(turn.tool_calls[0].raw_arguments) <= MAX_TOOL_ARGUMENT_BYTES
        # Truncated is not silently "complete": an over-long call is reported, never executed.
        assert turn.tool_calls[0].complete is False

    async def test_unbounded_reasoning_is_bounded_too(self) -> None:
        from openagent.providers.streaming import MAX_REASONING_BYTES, StreamingTurnAssembler

        assembler = StreamingTurnAssembler()
        chunk = "r" * 65536
        for _ in range((MAX_REASONING_BYTES // len(chunk)) + 10):
            assembler.append_reasoning(chunk)
        turn = assembler.build()
        assert turn.truncated is True
        assert len(turn.reasoning.encode("utf-8")) <= MAX_REASONING_BYTES

    def test_an_oversized_acp_message_is_a_protocol_failure_not_a_truncation(self) -> None:
        from openagent.runtimes.cli.acp import MAX_MESSAGE_BYTES, AcpProtocolError, decode

        oversized = b'{"jsonrpc":"2.0","id":1,"result":"' + b"x" * (MAX_MESSAGE_BYTES + 10) + b'"}'
        with pytest.raises(AcpProtocolError, match="ceiling"):
            decode(oversized)

    async def test_a_stream_of_only_malformed_frames_fails_rather_than_looking_empty(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            text="data: {broken\n\ndata: {also broken\n\n",
            headers={"content-type": "text/event-stream"},
        )
        result = await collect(adapter().stream_response(req(stream=True)))
        assert result.is_error


class TestAcpProtocolHardening:
    """The failure modes a one-way JSONL reader does not have (spec §18.3)."""

    def test_a_non_object_message_is_refused(self) -> None:
        from openagent.runtimes.cli.acp import AcpProtocolError, decode

        with pytest.raises(AcpProtocolError, match="not an object"):
            decode(b'["not", "an", "object"]')

    def test_an_unsupported_protocol_version_is_refused(self) -> None:
        from openagent.runtimes.cli.acp import AcpProtocolError, decode

        with pytest.raises(AcpProtocolError, match="jsonrpc version"):
            decode(b'{"jsonrpc":"1.0","id":1}')

    def test_undecodable_bytes_are_refused_not_skipped(self) -> None:
        """Silently dropping a message means a pending call waits for a reply already sent."""

        from openagent.runtimes.cli.acp import AcpProtocolError, decode

        with pytest.raises(AcpProtocolError):
            decode(b"\xff\xfe not json")

    def test_a_permission_request_defaults_to_refusing_an_unclassified_tool(self) -> None:
        """A tool nobody classified is a tool nobody reviewed."""

        from openagent.runtimes.cli.kimi_acp import permission_policy

        policy = permission_policy("safe-edit")
        assert policy.decide("some_new_tool_kind") is False

    def test_read_only_refuses_shell_and_writes(self) -> None:
        from openagent.runtimes.cli.kimi_acp import permission_policy

        policy = permission_policy("read-only")
        assert policy.decide("execute") is False
        assert policy.decide("write") is False
        assert policy.decide("read") is True

    def test_an_unknown_profile_gets_the_most_restrictive_policy(self) -> None:
        from openagent.runtimes.cli.kimi_acp import permission_policy

        policy = permission_policy("something-nobody-defined")
        assert policy.allow_execute is False
        assert policy.allow_edit is False

    def test_an_unrecognisable_permission_option_cancels_rather_than_guessing(self) -> None:
        """Guessing an option id when the decision is "deny" risks selecting an allow option."""

        from openagent.runtimes.cli.kimi_acp import _permission_response, permission_policy

        response = _permission_response(
            {"toolCall": {"kind": "execute"}, "options": [{"noOptionId": True}]},
            permission_policy("full"),
        )
        assert response["outcome"]["outcome"] == "cancelled"


# =========================================================================== boundaries


class TestTransportSecurity:
    def test_a_remote_endpoint_over_plain_http_is_refused(self) -> None:
        with pytest.raises(InsecureEndpointError, match="cleartext"):
            WireProviderAdapter(
                spec=get_spec("ollama"),  # type: ignore[arg-type]
                region="remote",
                base_url="http://ollama.lan:11434",
                api_key=SECRET,
            )

    def test_only_loopback_gets_the_plain_http_exemption(self) -> None:
        assert requires_tls("http://localhost:11434", local=True) is False
        assert requires_tls("http://ollama.lan:11434", local=True) is True
        # The substring trap: this host merely *contains* "localhost".
        assert requires_tls("http://localhost.attacker.example", local=True) is True

    def test_a_hosted_provider_gets_no_exemption_at_all(self) -> None:
        assert requires_tls("http://api.deepseek.com", local=False) is True

    async def test_a_tls_failure_is_reported_as_tls_and_never_retried(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """Retrying a certificate failure is how a TLS downgrade gets normalized as a flaky network."""

        httpx_mock.add_exception(
            httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        )
        result = await collect(adapter().stream_response(req()))
        assert result.error_type == ErrorType.TLS_ERROR.value
        assert not is_retryable(ErrorType.TLS_ERROR)


class TestRetryAmplification:
    def test_the_retry_budget_is_wall_clock_not_per_attempt(self) -> None:
        """Per-attempt budgets let three retries of a 120s call run for eight minutes."""

        from openagent.providers.retry import RetryBudget

        clock = iter([0.0, 50.0, 90.0, 130.0])
        budget = RetryBudget(120.0, clock=lambda: next(clock))
        assert budget.allows(10.0) is True
        assert budget.allows(60.0) is False

    @pytest.mark.parametrize(
        "error_type",
        [
            ErrorType.AUTHENTICATION_FAILED,
            ErrorType.PERMISSION_DENIED,
            ErrorType.INSUFFICIENT_BALANCE,
            ErrorType.INVALID_REQUEST,
            ErrorType.CONTEXT_LIMIT,
            ErrorType.MODEL_NOT_FOUND,
            ErrorType.TLS_ERROR,
        ],
    )
    def test_a_settled_rejection_is_never_retried(self, error_type: ErrorType) -> None:
        """Retrying converts one clear failure into several slow identical ones, on the user's quota."""

        assert not is_retryable(error_type)

    def test_a_partially_delivered_stream_is_never_replayed(self) -> None:
        """Replaying would duplicate text, tool calls and file changes."""

        assert not is_retryable(ErrorType.CONNECTION_LOST)

    def test_the_retryable_and_non_retryable_sets_stay_disjoint(self) -> None:
        from openagent.core.errors import NON_RETRYABLE, RETRYABLE

        assert RETRYABLE & NON_RETRYABLE == set()


# =========================================================================== tools


class TestToolExecutionPreconditions:
    """A tool call runs only when it is schema-valid, bounded and identified (spec §27.1)."""

    def test_an_unparseable_tool_call_never_becomes_executable(self) -> None:
        from openagent.providers.streaming import StreamingTurnAssembler

        assembler = StreamingTurnAssembler()
        assembler.append_tool_name("ping", index=0, tool_id="c1")
        assembler.append_tool_argument_fragment('{"value": ', index=0)
        turn = assembler.build()
        assert turn.tool_calls[0].complete is False
        assert turn.tool_calls[0].arguments == {}

    def test_arguments_that_are_not_an_object_are_refused(self) -> None:
        from openagent.providers.streaming import StreamingTurnAssembler

        assembler = StreamingTurnAssembler()
        assembler.append_tool_name("ping", index=0, tool_id="c1")
        assembler.append_tool_argument_fragment('"just a string"', index=0)
        assert assembler.build().tool_calls[0].complete is False

    def test_a_nameless_tool_call_is_refused(self) -> None:
        from openagent.providers.streaming import StreamingTurnAssembler

        assembler = StreamingTurnAssembler()
        assembler.append_tool_argument_fragment("{}", index=0, tool_id="c1")
        assert assembler.build().tool_calls[0].complete is False

    def test_a_structurally_broken_schema_marks_the_tool_not_executable(self) -> None:
        from openagent.providers.compat.profiles_v2 import get_profile
        from openagent.providers.tool_schema import normalize_tool_schema

        result = normalize_tool_schema(
            {"name": "bad", "parameters": {"type": "array"}}, get_profile("deepseek")
        )
        assert result.executable is False

    def test_a_dropped_constraint_is_reported_rather_than_applied_silently(self) -> None:
        """Removing `pattern` widens what the model is told it may send. Local validation still binds."""

        from openagent.providers.compat.profiles_v2 import CompatibilityProfile
        from openagent.providers.tool_schema import normalize_tool_schema

        profile = CompatibilityProfile("x", schema_unsupported_keywords=frozenset({"pattern"}))
        result = normalize_tool_schema(
            {
                "name": "ping",
                "parameters": {
                    "type": "object",
                    "properties": {"v": {"type": "string", "pattern": "^a+$"}},
                },
            },
            profile,
        )
        assert result.narrows_validation is True
        assert result.incompatible_keywords

    def test_a_tool_withheld_from_the_wire_is_reported_to_the_caller(self) -> None:
        from openagent.providers.compat.profiles_v2 import get_profile
        from openagent.providers.transport import Transport as T
        from openagent.providers.wire.openai_chat import OpenAIChatWire

        wire = OpenAIChatWire(
            profile=get_profile("deepseek"), transport=T(base_url="https://api.test", headers={})
        )
        payload, prep = wire.build_payload(
            req(tools=[{"name": "bad name!", "parameters": {"type": "object", "properties": {}}}]),
            stream=False,
        )
        assert "tools" not in payload
        assert prep.rejected == ["bad name!"]


class TestParallelToolPolicy:
    """Which tools may run concurrently (spec §27.2)."""

    def test_the_profiles_still_distinguish_read_only_from_write_capable(self) -> None:
        """Any parallel-execution decision has to be built on this distinction.

        Read-only tools are the ones that may run concurrently; writes, shell commands and same-file
        edits are sequential because their order is part of their meaning (spec §27.2).
        """

        from openagent.core.permissions import PROFILES

        assert {"read-only", "safe-edit", "full-access"} <= set(PROFILES)
        read_only = PROFILES["read-only"]
        assert "no edits" in read_only.description.lower()
        assert "no command execution" in read_only.description.lower()


# =========================================================================== artifacts


class TestContinuationArtifactSafety:
    def test_a_payload_containing_a_key_shaped_token_is_refused(self, tmp_path) -> None:
        """Refused rather than redacted: replay needs byte fidelity, so editing it would corrupt it."""

        from openagent.providers.continuation_store import (
            ContinuationStore,
            ContinuationStoreError,
        )

        store = ContinuationStore(tmp_path / "sessions")
        envelope = ContinuationEnvelope.build(
            provider_type="deepseek",
            protocol=Protocol.OPENAI_CHAT,
            strategy=ContinuationStrategy.NATIVE_MESSAGE_REPLAY,
            native_assistant_message={"role": "assistant", "content": f"key is {SECRET}"},
        )
        with pytest.raises(ContinuationStoreError):
            store.save("session-1", envelope)

    def test_a_traversing_session_id_is_refused(self, tmp_path) -> None:
        from openagent.providers.continuation_store import (
            ContinuationStore,
            ContinuationStoreError,
        )

        store = ContinuationStore(tmp_path / "sessions")
        envelope = ContinuationEnvelope.build(
            provider_type="deepseek",
            protocol=Protocol.OPENAI_CHAT,
            strategy=ContinuationStrategy.NORMALIZED_HISTORY,
        )
        for bad in ("../escape", "a/b", "..", "/absolute"):
            with pytest.raises(ContinuationStoreError):
                store.save(bad, envelope)

    def test_an_envelope_over_the_ceiling_is_refused_at_build_time(self) -> None:
        """Refused where a caller can still fall back, not mid-resume."""

        from openagent.providers.continuation import ContinuationError

        with pytest.raises(ContinuationError, match="ceiling"):
            ContinuationEnvelope.build(
                provider_type="deepseek",
                protocol=Protocol.OPENAI_CHAT,
                strategy=ContinuationStrategy.NATIVE_MESSAGE_REPLAY,
                native_assistant_message={"role": "assistant", "content": "x" * 400_000},
            )


class TestLocalProviderApproval:
    """Nothing changes the user's machine without being told to (spec §12.4, §13.4)."""

    @pytest.mark.parametrize(
        ("manager", "method", "args"),
        [
            ("ollama", "pull", ("qwen3:8b",)),
            ("ollama", "stop", ("qwen3:8b",)),
            ("lmstudio", "load", ("qwen3-8b",)),
            ("lmstudio", "download", ("qwen3-8b",)),
            ("lmstudio", "daemon_up", ()),
            ("lmstudio", "server_start", ()),
        ],
    )
    async def test_a_mutating_action_without_approval_raises(
        self, manager: str, method: str, args: tuple
    ) -> None:
        from openagent.providers.local_managers import (
            ApprovalRequiredError,
            LmStudioProviderManager,
            OllamaProviderManager,
        )

        instance = OllamaProviderManager() if manager == "ollama" else LmStudioProviderManager()
        with pytest.raises(ApprovalRequiredError):
            await getattr(instance, method)(*args)
