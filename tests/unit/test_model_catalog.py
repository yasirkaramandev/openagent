"""The shared model-catalog layer (spec §7, §11.1, §12.1, §13.1, §14.4, §16.4).

One discovery entry point per strategy, so nine providers do not grow nine ways of reporting the same
four outcomes. The outcomes are what these tests are about, because each one is a different message to
the user and three of them are routinely collapsed into "no models found":

* a **valid empty** catalog — the provider has nothing to offer;
* a **partial** catalog — some entries parsed, some did not, and the usable ones are still offered;
* an **unreadable** catalog — the request failed, and the wizard must offer retry / cache / manual;
* **manual-only** — this provider has no listable catalog at all, which is a supported configuration
  and not a failure.

The second theme: a catalog *claim* is evidence with a source, never a fact. OpenRouter saying a model
supports tools is recorded as ``PROVIDER_CATALOG`` and must lose to a live probe.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from openagent.core.models import DiscoveryStrategy
from openagent.providers.compat.evidence import (
    Capability,
    CapabilityStatus,
    EvidenceSource,
)
from openagent.providers.model_catalog import (
    CURATED_MANIFEST_VERSION,
    CatalogResult,
    discover_catalog,
)
from openagent.providers.transport import Transport

BASE = "https://api.test/v1"


def transport(base: str = BASE) -> Transport:
    return Transport(base_url=base, headers={}, max_retries=0)


class TestOpenAIModels:
    async def test_a_conventional_catalog_is_parsed(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(json={"data": [{"id": "m-1", "owned_by": "acme"}, {"id": "m-2"}]})
        result = await discover_catalog(DiscoveryStrategy.OPENAI_MODELS, transport())
        assert result.ok is True
        assert [entry.model.id for entry in result.entries] == ["m-1", "m-2"]
        assert result.manual_only is False

    async def test_a_valid_empty_catalog_is_a_success_not_a_failure(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(json={"data": []})
        result = await discover_catalog(DiscoveryStrategy.OPENAI_MODELS, transport())
        assert result.ok is True
        assert result.entries == []
        assert result.error_type is None

    async def test_malformed_entries_yield_a_partial_catalog_keeping_the_usable_ones(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(json={"data": [{"id": "good"}, {"no_id": True}, 42]})
        result = await discover_catalog(DiscoveryStrategy.OPENAI_MODELS, transport())
        assert result.partial is True
        assert result.ok is False
        assert [entry.model.id for entry in result.entries] == ["good"]

    async def test_an_unreadable_catalog_is_classified_not_emptied(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(status_code=401, json={"error": {"message": "bad key"}})
        result = await discover_catalog(DiscoveryStrategy.OPENAI_MODELS, transport())
        assert result.ok is False
        assert result.entries == []
        assert result.error_type == "unauthorized"

    async def test_the_credential_never_appears_in_the_error_message(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            status_code=401,
            json={"error": {"message": "Bearer sk-abcdef1234567890abcdef1234567890 rejected"}},
        )
        result = await discover_catalog(DiscoveryStrategy.OPENAI_MODELS, transport())
        assert "sk-abcdef1234567890abcdef1234567890" not in (result.error_message or "")


class TestManualOnly:
    async def test_manual_only_is_a_supported_outcome_with_no_request_made(self) -> None:
        result = await discover_catalog(DiscoveryStrategy.MANUAL_ONLY, transport())
        assert result.manual_only is True
        assert result.ok is True
        assert result.entries == []
        assert result.error_type is None

    def test_manual_only_reads_as_a_configuration_not_a_problem(self) -> None:
        result = CatalogResult(source="manual-only", ok=True, manual_only=True)
        assert "manual" in result.summary().lower()
        assert "fail" not in result.summary().lower()


class TestOpenRouterCatalog:
    def _payload(self) -> dict:
        return {
            "data": [
                {
                    "id": "vendor/model-a",
                    "canonical_slug": "vendor/model-a",
                    "name": "Model A",
                    "context_length": 128000,
                    "architecture": {
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                    },
                    "supported_parameters": ["tools", "reasoning", "structured_outputs"],
                    "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                },
                {
                    "id": "vendor/model-b",
                    "name": "Model B",
                    "context_length": 8192,
                    "architecture": {"input_modalities": ["text"]},
                    "supported_parameters": ["max_tokens"],
                    "pricing": {"prompt": "0", "completion": "0"},
                },
            ]
        }

    async def test_catalog_metadata_becomes_provider_catalog_evidence(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(json=self._payload())
        result = await discover_catalog(DiscoveryStrategy.OPENROUTER_CATALOG, transport())
        a = next(e for e in result.entries if e.model.id == "vendor/model-a")

        ledger = a.ledger()
        assert ledger.supports(Capability.TOOL_CALLING) is True
        assert ledger.source_of(Capability.TOOL_CALLING) is EvidenceSource.PROVIDER_CATALOG
        assert ledger.supports(Capability.REASONING) is True
        assert ledger.supports(Capability.IMAGE_INPUT) is True

    async def test_a_model_not_advertising_tools_is_unsupported_not_unknown(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """OpenRouter's supported_parameters is exhaustive, so absence is a real negative here."""

        httpx_mock.add_response(json=self._payload())
        result = await discover_catalog(DiscoveryStrategy.OPENROUTER_CATALOG, transport())
        b = next(e for e in result.entries if e.model.id == "vendor/model-b")
        assert b.ledger().status(Capability.TOOL_CALLING) is CapabilityStatus.UNSUPPORTED

    async def test_context_window_and_pricing_are_preserved_for_filtering(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(json=self._payload())
        result = await discover_catalog(DiscoveryStrategy.OPENROUTER_CATALOG, transport())
        a = next(e for e in result.entries if e.model.id == "vendor/model-a")
        assert a.model.context_window == 128000
        assert a.prompt_price == pytest.approx(0.000001)

    async def test_a_deprecated_entry_is_flagged_rather_than_hidden(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            json={
                "data": [
                    {
                        "id": "old/model",
                        "name": "Old",
                        "context_length": 4096,
                        "supported_parameters": [],
                        "deprecated": True,
                    }
                ]
            }
        )
        result = await discover_catalog(DiscoveryStrategy.OPENROUTER_CATALOG, transport())
        assert result.entries[0].deprecated is True


class TestOllamaTagsAndShow:
    async def test_tags_show_and_ps_are_combined(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            json={
                "models": [
                    {
                        "name": "qwen3:8b",
                        "digest": "sha256:abc",
                        "size": 5_200_000_000,
                        "modified_at": "2026-01-02T03:04:05Z",
                        "details": {
                            "family": "qwen3",
                            "parameter_size": "8B",
                            "quantization_level": "Q4_K_M",
                        },
                    }
                ]
            },
        )
        httpx_mock.add_response(
            url="http://localhost:11434/api/ps",
            json={"models": [{"name": "qwen3:8b", "size_vram": 5_000_000_000}]},
        )
        httpx_mock.add_response(
            url="http://localhost:11434/api/show",
            json={
                "capabilities": ["completion", "tools", "thinking"],
                "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 40960},
            },
        )
        result = await discover_catalog(
            DiscoveryStrategy.OLLAMA_TAGS_SHOW, transport("http://localhost:11434")
        )
        entry = result.entries[0]
        assert entry.model.id == "qwen3:8b"
        assert entry.loaded is True
        assert entry.model.context_window == 40960
        assert entry.revision == "sha256:abc"
        ledger = entry.ledger()
        assert ledger.supports(Capability.TOOL_CALLING) is True
        assert ledger.supports(Capability.REASONING) is True
        assert ledger.source_of(Capability.TOOL_CALLING) is EvidenceSource.PROVIDER_CATALOG

    async def test_capabilities_come_from_show_metadata_never_from_the_name(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """A model called "qwen3-tools" does not thereby support tools (spec §12.5)."""

        httpx_mock.add_response(
            url="http://localhost:11434/api/tags",
            json={"models": [{"name": "qwen3-tools-thinking:8b", "digest": "d"}]},
        )
        httpx_mock.add_response(url="http://localhost:11434/api/ps", json={"models": []})
        httpx_mock.add_response(url="http://localhost:11434/api/show", json={})
        result = await discover_catalog(
            DiscoveryStrategy.OLLAMA_TAGS_SHOW, transport("http://localhost:11434")
        )
        ledger = result.entries[0].ledger()
        assert ledger.supports(Capability.TOOL_CALLING) is None
        assert ledger.supports(Capability.REASONING) is None

    async def test_a_failing_show_leaves_the_model_listed_without_claims(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            url="http://localhost:11434/api/tags", json={"models": [{"name": "m:1"}]}
        )
        httpx_mock.add_response(url="http://localhost:11434/api/ps", json={"models": []})
        httpx_mock.add_response(url="http://localhost:11434/api/show", status_code=500)
        result = await discover_catalog(
            DiscoveryStrategy.OLLAMA_TAGS_SHOW, transport("http://localhost:11434")
        )
        assert [e.model.id for e in result.entries] == ["m:1"]
        assert result.partial is True

    async def test_an_unreachable_daemon_is_reported_as_such(self, httpx_mock: HTTPXMock) -> None:
        import httpx

        httpx_mock.add_exception(httpx.ConnectError("connection refused"))
        result = await discover_catalog(
            DiscoveryStrategy.OLLAMA_TAGS_SHOW, transport("http://localhost:11434")
        )
        assert result.ok is False
        assert result.error_type in {"network", "local_server_unavailable"}


class TestLmStudioNative:
    async def test_the_native_endpoint_is_used_when_present(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="http://localhost:1234/api/v0/models",
            json={
                "data": [
                    {
                        "id": "qwen3-8b",
                        "type": "llm",
                        "publisher": "qwen",
                        "arch": "qwen3",
                        "quantization": "Q4_K_M",
                        "state": "loaded",
                        "max_context_length": 32768,
                    }
                ]
            },
        )
        result = await discover_catalog(
            DiscoveryStrategy.LMSTUDIO_NATIVE, transport("http://localhost:1234")
        )
        entry = result.entries[0]
        assert entry.model.id == "qwen3-8b"
        assert entry.loaded is True
        assert entry.model.context_window == 32768
        assert entry.revision == "qwen3/Q4_K_M"
        assert "v0" in result.source

    async def test_a_build_without_the_native_path_falls_back_and_says_which_answered(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """The native path differs across LM Studio builds, so it is probed rather than assumed."""

        httpx_mock.add_response(url="http://localhost:1234/api/v0/models", status_code=404)
        httpx_mock.add_response(
            url="http://localhost:1234/api/v1/models", json={"data": [{"id": "m"}]}
        )
        result = await discover_catalog(
            DiscoveryStrategy.LMSTUDIO_NATIVE, transport("http://localhost:1234")
        )
        assert [e.model.id for e in result.entries] == ["m"]
        assert "v1" in result.source

    async def test_embedding_models_are_kept_but_marked_non_chat(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            url="http://localhost:1234/api/v0/models",
            json={"data": [{"id": "nomic-embed", "type": "embeddings", "state": "not-loaded"}]},
        )
        result = await discover_catalog(
            DiscoveryStrategy.LMSTUDIO_NATIVE, transport("http://localhost:1234")
        )
        entry = result.entries[0]
        assert entry.chat_capable is False
        assert entry.ledger().status(Capability.TEXT) is CapabilityStatus.UNSUPPORTED


class TestCuratedCatalog:
    async def test_a_curated_manifest_is_versioned_and_labelled_as_a_preset(self) -> None:
        result = await discover_catalog(
            DiscoveryStrategy.CURATED_CATALOG, transport(), provider_type="qwen"
        )
        assert result.ok is True
        assert result.entries, "the qwen manifest should not be empty"
        assert result.manifest_version == CURATED_MANIFEST_VERSION
        ledger = result.entries[0].ledger()
        # A curated preset is the weakest source and must be labelled as one, so a live probe wins.
        for capability in ledger.entries:
            assert ledger.source_of(capability) is EvidenceSource.CURATED_PRESET

    async def test_an_unknown_provider_has_no_manifest_and_says_so(self) -> None:
        result = await discover_catalog(
            DiscoveryStrategy.CURATED_CATALOG, transport(), provider_type="nobody"
        )
        assert result.entries == []
        assert result.manual_only is True

    def test_the_manifest_is_data_not_code(self) -> None:
        """A manifest that has to be edited in Python is one nobody dares update."""

        from openagent.providers.model_catalog import _CURATED

        assert json.dumps(_CURATED), "the curated manifest must be JSON-serializable"
