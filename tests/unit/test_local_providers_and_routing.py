"""Local provider managers, OpenRouter routing, and capability-aware filtering.

Three separate concerns, one test file, because each is small and each protects a rule that is easy to
regress into a convenience:

* a local provider manager must never change the user's machine without being told to (spec §12.4, §13.4);
* a routing policy is configuration, not a credential, and is never inferred (spec §14.3);
* a filter must keep "we do not know" as a third answer rather than rounding it (spec §14.2, §22.3).
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from openagent.core.errors import ErrorType
from openagent.core.models import RemoteModel
from openagent.providers.compat.evidence import Capability, CapabilityStatus, EvidenceSource
from openagent.providers.local_managers import (
    ApprovalRequiredError,
    LmStudioProviderManager,
    OllamaProviderManager,
    _minimal_env,
)
from openagent.providers.model_catalog import CatalogEntry
from openagent.providers.model_filter import ModelFilter, apply_filter
from openagent.providers.openrouter import (
    MaxPrice,
    OpenRouterRoutePolicy,
    policy_from_mapping,
)
from openagent.providers.transport import Transport


def local_transport(base: str) -> Transport:
    return Transport(base_url=base, headers={}, max_retries=0)


# =========================================================================== approval gate


class TestNothingHappensWithoutApproval:
    """The rule this module exists for. Each of these actions costs the user something real."""

    async def test_ollama_refuses_to_pull_without_approval(self) -> None:
        manager = OllamaProviderManager()
        with pytest.raises(ApprovalRequiredError, match="approval"):
            await manager.pull("qwen3:8b")

    async def test_ollama_refuses_to_unload_without_approval(self) -> None:
        with pytest.raises(ApprovalRequiredError):
            await OllamaProviderManager().stop("qwen3:8b")

    @pytest.mark.parametrize(
        "action",
        ["daemon_up", "server_start"],
    )
    async def test_lmstudio_refuses_to_start_things_without_approval(self, action: str) -> None:
        manager = LmStudioProviderManager()
        with pytest.raises(ApprovalRequiredError):
            await getattr(manager, action)()

    async def test_lmstudio_refuses_to_load_or_download_without_approval(self) -> None:
        manager = LmStudioProviderManager()
        with pytest.raises(ApprovalRequiredError):
            await manager.load("qwen3-8b")
        with pytest.raises(ApprovalRequiredError):
            await manager.download("qwen3-8b")

    async def test_the_error_names_the_action_so_a_prompt_can_quote_it(self) -> None:
        with pytest.raises(ApprovalRequiredError) as caught:
            await OllamaProviderManager().pull("llama4:70b")
        assert "llama4:70b" in caught.value.action

    async def test_approval_is_a_parameter_not_a_stored_setting(self) -> None:
        """A flag gets set once and forgotten; a parameter has to be passed by a caller with a user."""

        import inspect

        for method in ("pull", "stop"):
            signature = inspect.signature(getattr(OllamaProviderManager, method))
            assert signature.parameters["approved"].default is False
            assert signature.parameters["approved"].kind is inspect.Parameter.KEYWORD_ONLY


class TestSubprocessEnvironmentIsMinimal:
    def test_no_provider_credential_reaches_a_local_probe(self, monkeypatch) -> None:
        """A version probe has no business seeing the user's API keys (spec §19.2)."""

        monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-propagate")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-propagate")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-should-not-propagate")
        env = _minimal_env()
        assert "OPENAI_API_KEY" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert "DEEPSEEK_API_KEY" not in env
        assert "PATH" in env

    def test_path_is_always_present_even_with_an_empty_environment(self, monkeypatch) -> None:
        monkeypatch.delenv("PATH", raising=False)
        assert _minimal_env()["PATH"]


# =========================================================================== status reporting


class TestOllamaStatus:
    async def test_a_running_daemon_reports_models_and_residency(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(url="http://localhost:11434/api/version", json={"version": "0.6.2"})
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            json={
                "models": [
                    {
                        "name": "qwen3:8b",
                        "size": 5_200_000_000,
                        "details": {"quantization_level": "Q4_K_M", "parameter_size": "8B"},
                    },
                    {"name": "llama3:8b", "size": 4_700_000_000},
                ]
            },
        )
        httpx_mock.add_response(
            url="http://localhost:11434/api/ps",
            json={"models": [{"name": "qwen3:8b", "size_vram": 5_000_000_000}]},
        )
        status = await OllamaProviderManager(
            transport=local_transport("http://localhost:11434")
        ).status()

        assert status.server_reachable is True
        assert status.server_version == "0.6.2"
        assert len(status.models) == 2
        assert [m.id for m in status.loaded_models] == ["qwen3:8b"]
        assert status.resident_bytes == 5_000_000_000
        assert status.usable is True
        assert status.remediation() == []

    async def test_a_down_daemon_says_start_it_not_wait(self, httpx_mock: HTTPXMock) -> None:
        import httpx

        httpx_mock.add_exception(httpx.ConnectError("refused"))
        status = await OllamaProviderManager(
            transport=local_transport("http://localhost:11434")
        ).status()
        assert status.server_reachable is False
        assert status.error_type is ErrorType.LOCAL_SERVER_UNAVAILABLE
        assert status.usable is False
        assert status.remediation(), "a down server must come with something the user can do"

    async def test_a_running_daemon_with_no_models_says_download_one(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(url="http://localhost:11434/api/version", json={"version": "0.6.2"})
        httpx_mock.add_response(url="http://localhost:11434/api/tags", json={"models": []})
        httpx_mock.add_response(url="http://localhost:11434/api/ps", json={"models": []})
        status = await OllamaProviderManager(
            transport=local_transport("http://localhost:11434")
        ).status()
        assert status.server_reachable is True
        assert status.usable is False
        assert any("pull" in hint or "download" in hint for hint in status.remediation())

    async def test_residency_is_unknown_rather_than_zero_when_ps_fails(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(url="http://localhost:11434/api/version", json={"version": "0.6.2"})
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags", json={"models": [{"name": "m"}]}
        )
        httpx_mock.add_response(url="http://localhost:11434/api/ps", status_code=500)
        status = await OllamaProviderManager(
            transport=local_transport("http://localhost:11434")
        ).status()
        assert status.resident_bytes is None
        assert status.loaded_models == []


class TestLmStudioStatus:
    async def test_the_native_endpoint_is_preferred(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="http://localhost:1234/api/v0/models",
            json={
                "data": [
                    {
                        "id": "qwen3-8b",
                        "state": "loaded",
                        "quantization": "Q4_K_M",
                        "max_context_length": 32768,
                    },
                    {"id": "other", "state": "not-loaded"},
                ]
            },
        )
        status = await LmStudioProviderManager(
            transport=local_transport("http://localhost:1234")
        ).status()
        assert status.server_reachable is True
        assert [m.id for m in status.loaded_models] == ["qwen3-8b"]
        assert status.models[0].context_window == 32768

    async def test_a_build_without_the_native_path_falls_back(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(url="http://localhost:1234/api/v0/models", status_code=404)
        httpx_mock.add_response(url="http://localhost:1234/api/v1/models", status_code=404)
        httpx_mock.add_response(url="http://localhost:1234/v1/models", json={"data": [{"id": "m"}]})
        status = await LmStudioProviderManager(
            transport=local_transport("http://localhost:1234")
        ).status()
        assert status.server_reachable is True
        assert [m.id for m in status.models] == ["m"]

    async def test_no_endpoint_answering_is_a_down_server(self, httpx_mock: HTTPXMock) -> None:
        for _ in range(3):
            httpx_mock.add_response(status_code=404)
        status = await LmStudioProviderManager(
            transport=local_transport("http://localhost:1234")
        ).status()
        assert status.server_reachable is False
        assert status.error_type is ErrorType.LOCAL_SERVER_UNAVAILABLE


# =========================================================================== routing policy


class TestOpenRouterRoutePolicy:
    def test_a_default_policy_sends_nothing(self) -> None:
        """No routing preference means no `provider` object — not an object full of defaults."""

        assert OpenRouterRoutePolicy().to_request_fields() == {}
        assert OpenRouterRoutePolicy().is_default is True

    def test_an_allowlist_and_order_are_serialized(self) -> None:
        policy = OpenRouterRoutePolicy(
            provider_order=("together", "fireworks"), allow_fallbacks=False
        )
        fields = policy.to_request_fields()["provider"]
        assert fields["order"] == ["together", "fireworks"]
        assert fields["allow_fallbacks"] is False

    def test_zero_data_retention_implies_denying_data_collection(self) -> None:
        fields = OpenRouterRoutePolicy(zero_data_retention=True).to_request_fields()["provider"]
        assert fields["data_collection"] == "deny"

    def test_a_provider_cannot_be_both_required_and_ignored(self) -> None:
        """Picking one silently would route requests somewhere the user believes is excluded."""

        with pytest.raises(ValueError, match="both required and ignored"):
            OpenRouterRoutePolicy(provider_only=("a", "b"), provider_ignore=("b",))

    @pytest.mark.parametrize("bad", ["cheapest", "PRICE", ""])
    def test_an_undocumented_sort_value_is_refused(self, bad: str) -> None:
        with pytest.raises(ValueError, match="sort must be"):
            OpenRouterRoutePolicy(sort=bad)

    def test_an_undocumented_data_policy_is_refused(self) -> None:
        with pytest.raises(ValueError, match="data_policy must be"):
            OpenRouterRoutePolicy(data_policy="maybe")

    def test_price_ceilings_are_passed_through(self) -> None:
        fields = OpenRouterRoutePolicy(
            max_price=MaxPrice(prompt=1.5, completion=3.0)
        ).to_request_fields()["provider"]
        assert fields["max_price"] == {"prompt": 1.5, "completion": 3.0}

    def test_an_empty_price_object_adds_no_field(self) -> None:
        assert OpenRouterRoutePolicy(max_price=MaxPrice()).to_request_fields() == {}

    def test_the_policy_carries_no_credential(self) -> None:
        """The whole reason it is a separate object (spec §14.3)."""

        fields = set(OpenRouterRoutePolicy.__dataclass_fields__)
        assert not {f for f in fields if "key" in f or "token" in f or "secret" in f}

    def test_the_description_is_readable_and_secret_free(self) -> None:
        policy = OpenRouterRoutePolicy(
            provider_only=("together",), zero_data_retention=True, sort="latency"
        )
        described = policy.describe()
        assert "together" in described
        assert "zero data retention" in described
        assert "latency" in described

    def test_a_default_policy_describes_itself_as_the_default(self) -> None:
        assert "default" in OpenRouterRoutePolicy().describe()


class TestPolicyRoundTrip:
    def test_a_stored_policy_is_rebuilt(self) -> None:
        original = OpenRouterRoutePolicy(
            provider_order=("a",),
            provider_ignore=("b",),
            allow_fallbacks=False,
            require_parameters=True,
            data_policy="deny",
            sort="price",
            max_price=MaxPrice(prompt=2.0),
        )
        rebuilt = policy_from_mapping(
            {
                "provider_order": ["a"],
                "provider_ignore": ["b"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_policy": "deny",
                "sort": "price",
                "max_price": {"prompt": 2.0},
            }
        )
        assert rebuilt == original

    def test_an_unknown_field_is_refused_rather_than_ignored(self) -> None:
        """A typo'd key would be dropped and the request routed on different terms than the file says."""

        with pytest.raises(ValueError, match="unknown routing policy fields"):
            policy_from_mapping({"provider_orderr": ["a"]})


# =========================================================================== filtering


def entry(
    model_id: str,
    *,
    context: int | None = None,
    claims: dict[Capability, CapabilityStatus] | None = None,
    deprecated: bool = False,
    prompt_price: float | None = None,
) -> CatalogEntry:
    return CatalogEntry(
        model=RemoteModel(id=model_id, display_name=model_id, context_window=context),
        claims=claims or {},
        deprecated=deprecated,
        prompt_price=prompt_price,
    )


class TestFilterKeepsUnknownAsAThirdAnswer:
    def test_a_known_supported_model_matches(self) -> None:
        result = apply_filter(
            [entry("a", claims={Capability.TOOL_CALLING: CapabilityStatus.SUPPORTED})],
            ModelFilter(requires_tools=True),
        )
        assert [e.model.id for e in result.matched] == ["a"]
        assert result.unknown == []

    def test_a_known_unsupported_model_is_excluded(self) -> None:
        result = apply_filter(
            [entry("a", claims={Capability.TOOL_CALLING: CapabilityStatus.UNSUPPORTED})],
            ModelFilter(requires_tools=True),
        )
        assert result.matched == []
        assert [e.model.id for e in result.excluded] == ["a"]

    def test_an_unknown_model_is_offered_separately_with_a_reason(self) -> None:
        """Excluding it hides working models; including it silently is a lie. So: a third bucket."""

        result = apply_filter([entry("a")], ModelFilter(requires_tools=True))
        assert result.matched == []
        assert [e.model.id for e in result.unknown] == ["a"]
        assert "probe" in " ".join(result.unknown_reasons["a"])

    def test_selectable_puts_matches_before_unverified(self) -> None:
        entries = [
            entry("unknown-one"),
            entry("known", claims={Capability.TOOL_CALLING: CapabilityStatus.SUPPORTED}),
        ]
        result = apply_filter(entries, ModelFilter(requires_tools=True))
        assert [e.model.id for e in result.selectable] == ["known", "unknown-one"]

    def test_a_deprecated_model_is_excluded_by_default_but_visible_when_asked(self) -> None:
        entries = [entry("old", deprecated=True)]
        assert apply_filter(entries, ModelFilter()).excluded
        assert apply_filter(entries, ModelFilter(exclude_deprecated=False)).matched


class TestFilterHardBounds:
    def test_a_context_window_below_the_minimum_is_excluded(self) -> None:
        result = apply_filter([entry("a", context=4096)], ModelFilter(min_context=8192))
        assert [e.model.id for e in result.excluded] == ["a"]

    def test_an_unstated_context_window_is_unverified_not_excluded(self) -> None:
        result = apply_filter([entry("a")], ModelFilter(min_context=8192))
        assert [e.model.id for e in result.unknown] == ["a"]

    def test_a_price_over_the_ceiling_is_excluded(self) -> None:
        result = apply_filter([entry("a", prompt_price=0.01)], ModelFilter(max_prompt_price=0.005))
        assert [e.model.id for e in result.excluded] == ["a"]

    def test_an_unstated_price_is_not_treated_as_free(self) -> None:
        """It is also not excluded — it is simply unknown, and the caller can see prompt_price is None."""

        result = apply_filter([entry("a")], ModelFilter(max_prompt_price=0.005))
        assert result.excluded == []
        assert result.matched[0].prompt_price is None

    def test_search_narrows_without_rejecting(self) -> None:
        result = apply_filter([entry("alpha"), entry("beta")], ModelFilter(search="alph"))
        assert [e.model.id for e in result.matched] == ["alpha"]
        assert result.excluded == []

    def test_an_empty_filter_matches_everything(self) -> None:
        result = apply_filter([entry("a"), entry("b")], ModelFilter())
        assert len(result.matched) == 2
        assert ModelFilter().is_empty is True


class TestFilterUsesEvidenceStrength:
    def test_a_probe_result_overrides_a_catalog_claim(self) -> None:
        """A catalog that says no, contradicted by a probe that saw it work, must not exclude."""

        from openagent.providers.compat.evidence import CapabilityEvidence

        catalog_entry = entry("a", claims={Capability.TOOL_CALLING: CapabilityStatus.UNSUPPORTED})
        ledger = catalog_entry.ledger().record(
            CapabilityEvidence(
                capability=Capability.TOOL_CALLING,
                status=CapabilityStatus.SUPPORTED,
                source=EvidenceSource.LIVE_PROBE,
            )
        )
        assert ledger.supports(Capability.TOOL_CALLING) is True
        assert ledger.source_of(Capability.TOOL_CALLING) is EvidenceSource.LIVE_PROBE
