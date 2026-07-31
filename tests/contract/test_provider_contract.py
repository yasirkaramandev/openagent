"""The battery every v0.2 API provider must pass (spec §24).

Parameterized over all nine providers rather than written nine times, because the point of the shared
wire/catalog/spec layers is that these answers cannot diverge by provider. A test written per adapter
proves nine adapters behave; a test written once and run nine times proves they behave *the same*,
which is the property that actually stops the next provider from reintroducing a fixed bug.

Everything here is offline. No test in this file needs a credential, and none of them assert anything
about a live endpoint — live verification is a separate, explicitly-labelled suite (spec §26.2), and a
missing credential is recorded there as ``BLOCKED_BY_CREDENTIAL``, never as a pass.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pytest_httpx import HTTPXMock

from openagent.core.errors import ErrorType, is_retryable
from openagent.core.models import Protocol
from openagent.providers.base import Message, NormalizedModelRequest, Role, collect
from openagent.providers.spec import SPECS, get_spec, requires_tls
from openagent.providers.transport import Transport
from openagent.providers.wire_adapter import (
    InsecureEndpointError,
    WireProviderAdapter,
    build_provider_adapter,
)

#: The nine providers spec §31.1 requires. Named explicitly rather than read from SPECS, so deleting
#: a provider from the registry fails this list instead of silently shrinking the suite.
PROVIDERS = (
    "gemini",
    "ollama",
    "lmstudio",
    "openrouter",
    "deepseek",
    "qwen",
    "kimi",
    "glm",
    "minimax",
)

#: Providers whose default protocol is one of the chat-shaped ones, so a single fixture body drives
#: text/tool/usage assertions for all of them.
OPENAI_CHAT_PROVIDERS = ("openrouter", "deepseek", "qwen", "kimi", "glm")

#: The subset whose catalog is an HTTP endpoint. Qwen is deliberately excluded: its discovery strategy
#: is a curated manifest, so it has no request that can fail and no "unreadable catalog" state.
CATALOG_HTTP_PROVIDERS = ("openrouter", "deepseek", "kimi", "glm")

PING_TOOL = {
    "name": "ping",
    "description": "probe",
    "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}},
}


def adapter(provider: str, **kwargs) -> WireProviderAdapter:
    """An adapter wired to a mock transport, with retries off so failures assert once."""

    spec = get_spec(provider)
    assert spec is not None
    protocol, url = spec.resolve(region_id=kwargs.pop("region", None))
    kwargs.setdefault(
        "transport",
        Transport(base_url=url.rstrip("/"), headers={}, max_retries=0, backoff_base=0.0),
    )
    return WireProviderAdapter(spec=spec, api_key="test-key", **kwargs)


def req(stream: bool = False, **kwargs) -> NormalizedModelRequest:
    fields = {
        "model": "test-model",
        "system": "be brief",
        "messages": [Message(role=Role.USER, content="hi")],
        "stream": stream,
    }
    fields.update(kwargs)
    return NormalizedModelRequest(**fields)  # type: ignore[arg-type]


# =========================================================================== registry


class TestEveryProviderIsRegistered:
    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_the_provider_has_a_spec(self, provider: str) -> None:
        assert get_spec(provider) is not None, f"{provider} is required by spec §31.1"

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_an_adapter_can_be_built_without_a_credential_present(self, provider: str) -> None:
        """Building must not require a live key: the wizard builds one to *test* a key."""

        built = build_provider_adapter(provider, api_key=None)
        assert built.protocol in built.spec.protocols

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_every_declared_protocol_has_an_endpoint_in_some_region(self, provider: str) -> None:
        """A declared protocol with no endpoint anywhere is a claim nothing can honour."""

        spec = get_spec(provider)
        assert spec is not None
        served = {p for region in spec.regions for p in region.endpoints}
        assert set(spec.protocols) <= served

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_the_default_region_exists(self, provider: str) -> None:
        spec = get_spec(provider)
        assert spec is not None
        assert spec.region(None) is not None

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_a_key_needing_provider_names_its_environment_variables(self, provider: str) -> None:
        spec = get_spec(provider)
        assert spec is not None
        if spec.needs_key:
            assert spec.env_vars, f"{provider} needs a key but documents no environment variable"


# =========================================================================== credentials


class TestCredentialHandling:
    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_the_credential_is_sent_as_a_header_never_in_the_url(self, provider: str) -> None:
        built = adapter(provider)
        assert "test-key" not in built.base_url
        assert "test-key" in json.dumps(dict(built.transport.headers))

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_no_credential_appears_in_the_describe_output(self, provider: str) -> None:
        """``describe()`` reaches Doctor output and logs, so it must be secret-free."""

        assert "test-key" not in json.dumps(adapter(provider).describe())

    @pytest.mark.parametrize("provider", ("ollama", "lmstudio"))
    def test_a_local_provider_needs_no_credential(self, provider: str) -> None:
        spec = get_spec(provider)
        assert spec is not None
        assert spec.needs_key is False
        assert spec.local is True

    @pytest.mark.parametrize("provider", ("ollama", "lmstudio"))
    def test_no_authorization_header_when_no_key_was_given(self, provider: str) -> None:
        built = WireProviderAdapter(spec=get_spec(provider), api_key=None)  # type: ignore[arg-type]
        assert "Authorization" not in built.transport.headers

    @pytest.mark.parametrize("provider", ("ollama", "lmstudio"))
    def test_a_local_provider_still_carries_a_key_the_caller_supplied(self, provider: str) -> None:
        """ "No credential required" is not "no credential accepted".

        A remote Ollama or LM Studio behind a reverse proxy takes a bearer token (spec §12.3,
        §13.4). Dropping it would send unauthenticated requests and report the proxy's 401 as a bad
        key.
        """

        built = adapter(provider)
        assert built.transport.headers["Authorization"] == "Bearer test-key"

    def test_the_anthropic_protocol_uses_its_own_auth_header(self) -> None:
        built = adapter("minimax")
        assert built.transport.headers["x-api-key"] == "test-key"
        assert "anthropic-version" in built.transport.headers
        assert "Authorization" not in built.transport.headers

    def test_gemini_uses_the_google_api_key_header(self) -> None:
        built = adapter("gemini")
        assert built.transport.headers["x-goog-api-key"] == "test-key"

    def test_a_workspace_id_is_scoped_by_header(self) -> None:
        built = adapter("qwen", workspace_id="ws-1")
        assert built.transport.headers["X-DashScope-WorkSpace"] == "ws-1"


class TestTlsPolicy:
    def test_loopback_local_providers_may_use_plain_http(self) -> None:
        assert requires_tls("http://localhost:11434", local=True) is False
        assert requires_tls("http://127.0.0.1:1234", local=True) is False

    def test_a_remote_local_provider_still_requires_tls(self) -> None:
        """A local provider reached over a network address is a remote connection (spec §23.3)."""

        assert requires_tls("http://ollama.lan:11434", local=True) is True

    def test_a_hosted_provider_never_gets_the_loopback_exemption(self) -> None:
        assert requires_tls("http://api.example.com", local=False) is True

    def test_building_an_insecure_remote_adapter_is_refused(self) -> None:
        with pytest.raises(InsecureEndpointError, match="cleartext"):
            build_provider_adapter(
                "ollama", region="remote", base_url="http://ollama.lan:11434", api_key="k"
            )

    def test_an_explicit_opt_in_is_required_and_sufficient(self) -> None:
        built = build_provider_adapter(
            "ollama",
            region="remote",
            base_url="http://ollama.lan:11434",
            api_key="k",
            allow_insecure_http=True,
        )
        assert built.base_url == "http://ollama.lan:11434"


# =========================================================================== regions


class TestRegions:
    @pytest.mark.parametrize("provider", ("qwen", "kimi", "glm", "minimax"))
    def test_a_region_bound_provider_offers_more_than_one(self, provider: str) -> None:
        spec = get_spec(provider)
        assert spec is not None
        assert len(spec.regions) >= 2, f"{provider} is documented as region-bound"

    @pytest.mark.parametrize("provider", ("qwen", "kimi", "glm", "minimax"))
    def test_the_same_protocol_has_a_different_url_in_each_region(self, provider: str) -> None:
        """Two regions sharing a URL for one protocol would make the region field decorative.

        Within a region, two protocols may legitimately share a base URL (Qwen serves Chat and
        Responses from the same compatible-mode root and distinguishes them by path), so uniqueness
        is asserted per protocol across regions rather than globally.
        """

        spec = get_spec(provider)
        assert spec is not None
        for protocol in spec.protocols:
            urls = [
                region.endpoints[protocol]
                for region in spec.regions
                if protocol in region.endpoints
            ]
            assert len(set(urls)) == len(urls), f"{provider}/{protocol.value} reuses a URL"

    def test_an_unknown_region_is_refused_rather_than_defaulted(self) -> None:
        """Falling back would point a China key at the international endpoint (spec §18.1)."""

        spec = get_spec("kimi")
        assert spec is not None
        with pytest.raises(ValueError, match="no region"):
            spec.resolve(region_id="mars")

    def test_a_protocol_a_region_does_not_serve_is_refused(self) -> None:
        spec = get_spec("openrouter")
        assert spec is not None
        with pytest.raises(ValueError, match="does not serve"):
            spec.resolve(protocol=Protocol.ANTHROPIC_MESSAGES)


# =========================================================================== catalog


class TestCatalogOutcomes:
    @pytest.mark.parametrize("provider", CATALOG_HTTP_PROVIDERS)
    async def test_an_unreadable_catalog_is_never_reported_as_empty(
        self, provider: str, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(status_code=500, json={"error": {"message": "boom"}})
        result = await adapter(provider).catalog()
        assert result.ok is False
        # The distinction the whole layer exists for: a failure is not an empty list.
        assert result.error_type is not None

    @pytest.mark.parametrize("provider", ("deepseek", "kimi", "glm"))
    async def test_a_valid_empty_catalog_is_a_success(
        self, provider: str, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(json={"data": []})
        result = await adapter(provider).catalog()
        assert result.ok is True
        assert result.entries == []

    @pytest.mark.parametrize("provider", ("deepseek", "kimi", "glm"))
    async def test_a_rejected_credential_is_reported_as_such_not_as_unreachable(
        self, provider: str, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(status_code=401, json={"error": {"message": "invalid key"}})
        health = await adapter(provider).test_connection()
        assert health.ok is False
        assert "rejected" in health.detail

    @pytest.mark.parametrize("provider", ("deepseek", "kimi"))
    async def test_an_endpoint_without_a_model_list_is_still_healthy(
        self, provider: str, httpx_mock: HTTPXMock
    ) -> None:
        """A provider that cannot list models is usable by typing an id; it is not broken."""

        httpx_mock.add_response(status_code=404, json={"error": {"message": "no such route"}})
        health = await adapter(provider).test_connection()
        assert health.ok is True

    async def test_a_manual_only_provider_is_healthy_without_any_request(self) -> None:
        health = await adapter("qwen").test_connection()
        assert health.ok is True

    async def test_manual_only_still_offers_the_curated_starting_list(self) -> None:
        result = await adapter("qwen").catalog()
        assert result.models, "the curated manifest gives the wizard something to show"

    @pytest.mark.parametrize("provider", CATALOG_HTTP_PROVIDERS)
    async def test_list_models_raises_on_an_unreadable_catalog(
        self, provider: str, httpx_mock: HTTPXMock
    ) -> None:
        """The flat contract is "a list or an error"; returning [] would be a lie."""

        httpx_mock.add_response(status_code=503, text="upstream down")
        from openagent.providers.transport import TransportError

        with pytest.raises(TransportError):
            await adapter(provider).list_models()


# =========================================================================== turns


def chat_body(*, tool: bool = False, reasoning: str | None = None) -> dict:
    message: dict = {"role": "assistant", "content": None if tool else "hello"}
    if reasoning:
        message["reasoning_content"] = reasoning
    if tool:
        message["tool_calls"] = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "ping", "arguments": '{"value": 1}'},
            }
        ]
    return {
        "id": "resp-1",
        "choices": [{"message": message, "finish_reason": "tool_calls" if tool else "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7},
    }


class TestTurns:
    @pytest.mark.parametrize("provider", OPENAI_CHAT_PROVIDERS)
    async def test_text_and_usage(self, provider: str, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(json=chat_body())
        result = await collect(adapter(provider).stream_response(req()))
        assert result.text == "hello"
        assert result.usage is not None
        assert (result.usage.input_tokens, result.usage.output_tokens) == (5, 7)

    @pytest.mark.parametrize("provider", OPENAI_CHAT_PROVIDERS)
    async def test_a_tool_call(self, provider: str, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(json=chat_body(tool=True))
        result = await collect(adapter(provider).stream_response(req(tools=[PING_TOOL])))
        assert not result.is_error
        assert [(c.name, c.arguments) for c in result.tool_calls] == [("ping", {"value": 1})]

    @pytest.mark.parametrize("provider", OPENAI_CHAT_PROVIDERS)
    async def test_streamed_text(self, provider: str, httpx_mock: HTTPXMock) -> None:
        body = (
            'data: {"id":"s","choices":[{"delta":{"content":"a"}}]}\n\n'
            'data: {"id":"s","choices":[{"delta":{"content":"b"}},{"delta":{}}]}\n\n'
            'data: {"id":"s","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        httpx_mock.add_response(text=body, headers={"content-type": "text/event-stream"})
        result = await collect(adapter(provider).stream_response(req(stream=True)))
        assert result.text == "ab"

    @pytest.mark.parametrize("provider", OPENAI_CHAT_PROVIDERS)
    async def test_utf8_split_across_fragments_is_reassembled(
        self, provider: str, httpx_mock: HTTPXMock
    ) -> None:
        """A multi-byte character split by the provider must not become two broken ones."""

        body = (
            'data: {"choices":[{"delta":{"content":"\\u00fc"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"\\u011f"}},{"delta":{}}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        httpx_mock.add_response(text=body, headers={"content-type": "text/event-stream"})
        result = await collect(adapter(provider).stream_response(req(stream=True)))
        assert result.text == "üğ"

    @pytest.mark.parametrize("provider", OPENAI_CHAT_PROVIDERS)
    async def test_invalid_tool_json_is_an_error_not_a_call(
        self, provider: str, httpx_mock: HTTPXMock
    ) -> None:
        body = chat_body(tool=True)
        body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{not json"
        httpx_mock.add_response(json=body)
        result = await collect(adapter(provider).stream_response(req(tools=[PING_TOOL])))
        assert result.is_error
        assert result.error_type == ErrorType.INVALID_TOOL_ARGUMENTS.value
        assert not result.tool_calls

    @pytest.mark.parametrize("provider", OPENAI_CHAT_PROVIDERS)
    async def test_a_tool_call_with_no_id_is_refused(
        self, provider: str, httpx_mock: HTTPXMock
    ) -> None:
        body = chat_body(tool=True)
        body["choices"][0]["message"]["tool_calls"][0]["id"] = ""
        httpx_mock.add_response(json=body)
        result = await collect(adapter(provider).stream_response(req(tools=[PING_TOOL])))
        assert result.is_error
        assert not result.tool_calls


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, ErrorType.AUTHENTICATION_FAILED),
            (403, ErrorType.PERMISSION_DENIED),
            (429, ErrorType.PROVIDER_RATE_LIMITED),
            (503, ErrorType.PROVIDER_OVERLOADED),
        ],
    )
    @pytest.mark.parametrize("provider", ("deepseek", "kimi", "glm", "openrouter"))
    async def test_statuses_map_consistently_across_providers(
        self, provider: str, status: int, expected: ErrorType, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(status_code=status, json={"error": {"message": "x"}})
        result = await collect(adapter(provider).stream_response(req()))
        assert result.error_type == expected.value

    @pytest.mark.parametrize("provider", OPENAI_CHAT_PROVIDERS)
    async def test_a_context_limit_is_never_retried(
        self, provider: str, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            status_code=400,
            json={"error": {"message": "This model's maximum context length is 8192 tokens"}},
        )
        result = await collect(adapter(provider).stream_response(req()))
        assert result.is_error
        assert not is_retryable(ErrorType(result.error_type))

    @pytest.mark.parametrize("provider", OPENAI_CHAT_PROVIDERS)
    async def test_a_provider_error_body_never_echoes_the_credential(
        self, provider: str, httpx_mock: HTTPXMock
    ) -> None:
        """Providers do echo the rejected Authorization header; the error must not carry it onward."""

        httpx_mock.add_response(
            status_code=401,
            json={"error": {"message": "Bearer sk-live-4f8a2c9e1b7d3a6f5e0c8b2d9a4f7e1c rejected"}},
        )
        result = await collect(adapter(provider).stream_response(req()))
        assert "sk-live-4f8a2c9e1b7d3a6f5e0c8b2d9a4f7e1c" not in (result.error_message or "")

    @pytest.mark.parametrize("provider", OPENAI_CHAT_PROVIDERS)
    async def test_a_timeout_is_classified_not_raised(
        self, provider: str, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_exception(httpx.ReadTimeout("slow"))
        result = await collect(adapter(provider).stream_response(req()))
        assert result.error_type == ErrorType.TIMEOUT.value

    @pytest.mark.parametrize("provider", OPENAI_CHAT_PROVIDERS)
    async def test_an_unreachable_endpoint_is_classified(
        self, provider: str, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_exception(httpx.ConnectError("refused"))
        result = await collect(adapter(provider).stream_response(req()))
        assert result.error_type == ErrorType.NETWORK_UNAVAILABLE.value

    async def test_a_tls_failure_is_reported_as_tls_and_not_retried(
        self, httpx_mock: HTTPXMock
    ) -> None:
        import ssl

        httpx_mock.add_exception(
            httpx.ConnectError("certificate verify failed", request=None)  # type: ignore[arg-type]
            if False
            else httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        )
        result = await collect(adapter("deepseek").stream_response(req()))
        assert result.error_type == ErrorType.TLS_ERROR.value
        assert not is_retryable(ErrorType.TLS_ERROR)
        assert ssl is not None


class TestCancellation:
    @pytest.mark.parametrize("provider", ("deepseek", "openrouter"))
    async def test_a_stalled_stream_can_be_cancelled(
        self, provider: str, httpx_mock: HTTPXMock
    ) -> None:
        """A provider that accepts a request and goes silent must not make Ctrl-C hang."""

        import asyncio

        async def stall(request):  # noqa: ANN001 - pytest_httpx callback signature
            await asyncio.sleep(30)
            return httpx.Response(200, json={})

        httpx_mock.add_callback(stall)
        built = adapter(provider)

        async def drain() -> None:
            async for _ in built.stream_response(req(stream=True)):
                pass

        task = asyncio.ensure_future(drain())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestCapabilityEvidence:
    @pytest.mark.parametrize("provider", ("deepseek", "kimi"))
    async def test_a_rejected_credential_leaves_capabilities_unknown(
        self, provider: str, httpx_mock: HTTPXMock
    ) -> None:
        """The failure mode this guards: one 401 marking a working model permanently incapable."""

        # Two responses: probe_evidence reads the catalog first, to bind its findings to a model
        # revision. Both are rejected here, which is the real-world shape of a bad key.
        httpx_mock.add_response(status_code=401, json={"error": {"message": "no"}})
        httpx_mock.add_response(status_code=401, json={"error": {"message": "no"}})
        outcome = await adapter(provider).probe_evidence("m")
        assert outcome.ran is False
        assert outcome.blocked_by is ErrorType.AUTHENTICATION_FAILED
        from openagent.providers.compat.evidence import Capability

        assert outcome.ledger.supports(Capability.TOOL_CALLING) is None

    async def test_a_probe_records_only_what_it_observed(self, httpx_mock: HTTPXMock) -> None:
        from openagent.providers.compat.evidence import Capability, EvidenceSource

        httpx_mock.add_response(json={"data": []})  # catalog
        httpx_mock.add_response(
            json={
                "id": "r",
                "choices": [{"message": {"role": "assistant", "content": "PROBE_OK_7F"}}],
            }
        )  # text
        httpx_mock.add_response(
            text='data: {"choices":[{"delta":{"content":"x"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"},
        )  # stream
        httpx_mock.add_response(
            json={"id": "r", "choices": [{"message": {"role": "assistant", "content": "no tools"}}]}
        )  # tools: the model declined
        outcome = await adapter("deepseek").probe_evidence("m")

        assert outcome.ran is True
        assert outcome.ledger.supports(Capability.TEXT) is True
        assert outcome.ledger.supports(Capability.SYSTEM_PROMPT) is True
        assert outcome.ledger.supports(Capability.STREAMING) is True
        assert outcome.ledger.source_of(Capability.TEXT) is EvidenceSource.LIVE_PROBE
        # The model was asked for a tool call and did not make one. That is not proof it cannot.
        assert outcome.ledger.supports(Capability.TOOL_CALLING) is None
        assert outcome.ledger.supports(Capability.PARALLEL_TOOL_CALLING) is None


class TestContinuation:
    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_every_provider_produces_a_bound_continuation(self, provider: str) -> None:
        """An envelope must name the provider and protocol it came from, or replay is unguarded."""

        built = adapter(provider)
        envelope = built.build_continuation(model_id="m")
        assert envelope.provider_type == built.spec.provider_type
        assert envelope.protocol is built.protocol

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_an_envelope_refuses_replay_into_a_different_provider(self, provider: str) -> None:
        from openagent.providers.continuation import ContinuationError

        built = adapter(provider)
        envelope = built.build_continuation(model_id="m")
        other = "glm" if provider != "glm" else "kimi"
        with pytest.raises(ContinuationError):
            envelope.verify(provider_type=other, protocol=built.protocol)

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_a_changed_model_warns_rather_than_refusing(self, provider: str) -> None:
        """Native material from one model into another half-works, so the user decides (spec §28)."""

        built = adapter(provider)
        envelope = built.build_continuation(model_id="model-a")
        warnings = envelope.verify(
            provider_type=built.spec.provider_type, protocol=built.protocol, model_id="model-b"
        )
        assert any("model-a" in warning for warning in warnings)


class TestSpecTableIntegrity:
    def test_no_provider_declares_server_state_without_a_protocol_that_has_it(self) -> None:
        stateful = {
            Protocol.OPENAI_RESPONSES,
            Protocol.GEMINI_INTERACTIONS,
        }
        for name, spec in SPECS.items():
            if spec.supports_server_state:
                assert stateful & set(spec.protocols), (
                    f"{name} claims server-side state but speaks no protocol that provides it"
                )

    def test_every_spec_has_a_documentation_url(self) -> None:
        """A provider row a user cannot look up is one they cannot debug."""

        for name, spec in SPECS.items():
            assert spec.docs_url.startswith("https://"), f"{name} has no docs URL"

    def test_local_providers_default_to_a_loopback_region(self) -> None:
        for name, spec in SPECS.items():
            if not spec.local:
                continue
            region = spec.region(None)
            assert region is not None
            assert all(
                "localhost" in url or "127.0.0.1" in url for url in region.endpoints.values()
            ), f"{name}'s default region should be this machine"


# =========================================================================== factory routing


class TestTheFactoryReachesTheV2Adapters:
    """Without this routing the whole v0.2 provider layer is unreachable from the app.

    ``build_adapter`` is the single chokepoint between a stored connection and something that can
    talk to it — provider_service and preflight both go through it — so a provider is either
    upgraded for both callers or for neither.
    """

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_every_v2_provider_is_served_by_the_wire_adapter(self, provider: str) -> None:
        from openagent.core.models import ProviderConnection
        from openagent.providers.factory import build_adapter

        connection = ProviderConnection(id="p", name="p", provider_type=provider)
        assert isinstance(build_adapter(connection, "test-key"), WireProviderAdapter)

    @pytest.mark.parametrize("provider", ["openai", "nvidia-build"])
    def test_v0_1_providers_keep_their_own_adapters(self, provider: str) -> None:
        """Those adapters are still the right implementation for them; nothing is churned."""

        from openagent.core.models import ProviderConnection
        from openagent.providers.factory import build_adapter

        connection = ProviderConnection(id="p", name="p", provider_type=provider)
        assert not isinstance(build_adapter(connection, "k"), WireProviderAdapter)

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_every_v2_provider_is_selectable_in_the_wizard(self, provider: str) -> None:
        """The wizard builds its list from PRESETS; an implemented provider missing from it is
        implemented and unreachable."""

        from openagent.providers.factory import preset_names

        assert provider in preset_names()

    def test_a_stored_region_is_honoured_over_the_spec_default(self) -> None:
        from openagent.core.models import ProviderConnection
        from openagent.providers.factory import build_adapter

        connection = ProviderConnection(id="p", name="p", provider_type="kimi", region="cn")
        built = build_adapter(connection, "k")
        assert built.region_id == "cn"
        assert "moonshot.cn" in built.base_url

    def test_a_stored_protocol_the_region_serves_is_honoured(self) -> None:
        from openagent.core.models import ProviderConnection
        from openagent.providers.factory import build_adapter

        connection = ProviderConnection(
            id="p", name="p", provider_type="minimax", protocol=Protocol.OPENAI_CHAT
        )
        built = build_adapter(connection, "k")
        assert built.protocol is Protocol.OPENAI_CHAT

    def test_a_stored_protocol_the_provider_cannot_speak_falls_back_to_its_preference(self) -> None:
        """Never served over a protocol the row did not ask for *and* the region does not serve.

        A row carrying a stale protocol is a real thing after an upgrade; using the spec's own
        preference order is the honest resolution, and it is why ``protocol`` is only passed through
        when the spec actually lists it.
        """

        from openagent.core.models import ProviderConnection
        from openagent.providers.factory import build_adapter

        connection = ProviderConnection(
            id="p", name="p", provider_type="gemini", protocol=Protocol.OPENAI_CHAT
        )
        built = build_adapter(connection, "k")
        assert built.protocol is Protocol.GEMINI_INTERACTIONS

    def test_a_stored_base_url_overrides_the_region_default(self) -> None:
        """A self-hosted endpoint or a gateway keeps working; the region still scopes the credential."""

        from openagent.core.models import ProviderConnection
        from openagent.providers.factory import build_adapter

        connection = ProviderConnection(
            id="p",
            name="p",
            provider_type="deepseek",
            base_url="https://gateway.example.com/v1",
        )
        built = build_adapter(connection, "k")
        assert built.base_url == "https://gateway.example.com/v1"

    def test_a_stored_row_cannot_grant_itself_the_insecure_http_exemption(self) -> None:
        """A user has to opt into cleartext deliberately; a persisted row is not that opt-in."""

        from openagent.core.models import ProviderConnection
        from openagent.providers.factory import build_adapter

        connection = ProviderConnection(
            id="p",
            name="p",
            provider_type="ollama",
            region="remote",
            base_url="http://ollama.lan:11434",
        )
        with pytest.raises(InsecureEndpointError):
            build_adapter(connection, "k")
