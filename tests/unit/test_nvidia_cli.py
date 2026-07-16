"""NVIDIA Build CLI flows (spec §17, §18, §19) — every command the README and the AI skill document.

These prove the terminal contract end to end without a real key: the key is only ever taken from a
hidden prompt or an env-var reference (never argv), the catalog is never presented as a capability
claim, ``provider test`` never claims the key is valid, and an unprobed model cannot silently become
an agent.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openagent.cli.app import app
from openagent.core.models import ModelCapabilities, RemoteModel
from openagent.providers.discovery import (
    PROBE_PARTIAL,
    PROBE_VERIFIED,
    AgentModelProbe,
)
from openagent.services.provider_service import ProviderService

runner = CliRunner()

FAKE_KEY = "nvapi-THIS_IS_A_FAKE_TEST_KEY_123456"

_CATALOG = [
    RemoteModel(id="nvidia/nemotron-test", display_name="nvidia/nemotron-test", owned_by="nvidia"),
    RemoteModel(id="nvidia/embed-test", display_name="nvidia/embed-test", owned_by="nvidia"),
    RemoteModel(id="meta/vision-test", display_name="meta/vision-test", owned_by="meta"),
    RemoteModel(
        id="deepseek-ai/chat-test", display_name="deepseek-ai/chat-test", owned_by="deepseek-ai"
    ),
]


@pytest.fixture(autouse=True)
def _in_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)


@pytest.fixture()
def catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _models(self, name):  # noqa: ANN001
        return _CATALOG

    monkeypatch.setattr(ProviderService, "remote_models", _models)


def _verified(model: str) -> AgentModelProbe:
    return AgentModelProbe(
        model,
        ModelCapabilities(text=True, streaming=True, tool_calling=True),
        True,
        PROBE_VERIFIED,
        "",
        datetime.now(timezone.utc),
    )


def _partial(model: str) -> AgentModelProbe:
    return AgentModelProbe(
        model,
        ModelCapabilities(text=True, streaming=True, tool_calling=None),
        False,
        PROBE_PARTIAL,
        "",
        datetime.now(timezone.utc),
    )


def _add_nvidia_via_env() -> object:
    return runner.invoke(
        app,
        [
            "provider",
            "add",
            "nvidia-build",
            "--type",
            "nvidia-build",
            "--key-env",
            "NVIDIA_API_KEY",
        ],
    )


# --------------------------------------------------------------------------- §17.1/§17.2 provider add


def test_provider_add_prompts_for_the_key_and_never_takes_it_as_an_argument():
    result = runner.invoke(
        app,
        ["provider", "add", "nvidia-build", "--type", "nvidia-build"],
        input=f"{FAKE_KEY}\n",
    )
    assert result.exit_code == 0, result.stdout
    # The prompt is labelled with the provider's own credential label (§17.1)…
    assert "NVIDIA API Key for nvidia-build" in result.stdout
    # …and the key itself is never echoed back.
    assert FAKE_KEY not in result.stdout
    listed = runner.invoke(app, ["provider", "list"])
    assert "nvidia-build" in listed.stdout


def test_provider_add_with_env_var_reference():
    result = _add_nvidia_via_env()
    assert result.exit_code == 0, result.stdout
    listed = runner.invoke(app, ["provider", "list", "--json"])
    data = json.loads(listed.stdout)
    entry = next(p for p in data if p["name"] == "nvidia-build")
    assert entry["credential"]["type"] == "env"
    assert entry["credential"]["env_var"] == "NVIDIA_API_KEY"
    assert entry["provider_type"] == "nvidia-build"
    # The base URL comes from the preset, not from the user.
    assert entry["protocol"] == "openai-chat"


def test_nvidia_preset_is_listed():
    result = runner.invoke(app, ["provider", "presets"])
    assert result.exit_code == 0
    assert "NVIDIA Build" in result.stdout
    assert "nvidia-build" in result.stdout


# --------------------------------------------------------------------------- §17.3 model listing


def test_provider_models_json_is_machine_readable_and_claims_no_capabilities(catalog):
    _add_nvidia_via_env()
    result = runner.invoke(app, ["provider", "models", "nvidia-build", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)  # must parse: Rich must not wrap/corrupt it
    assert payload["provider"] == "nvidia-build"
    assert [m["id"] for m in payload["models"]] == [m.id for m in _CATALOG]
    assert [m["owned_by"] for m in payload["models"]] == ["nvidia", "nvidia", "meta", "deepseek-ai"]
    # No catalog entry is ever presented as agent-compatible (§14.3).
    assert all(m["capabilities"] is None for m in payload["models"])


def test_provider_models_search_and_owner_filters(catalog):
    _add_nvidia_via_env()
    searched = runner.invoke(
        app, ["provider", "models", "nvidia-build", "--search", "nemotron", "--json"]
    )
    assert [m["id"] for m in json.loads(searched.stdout)["models"]] == ["nvidia/nemotron-test"]

    owned = runner.invoke(app, ["provider", "models", "nvidia-build", "--owner", "meta", "--json"])
    assert [m["id"] for m in json.loads(owned.stdout)["models"]] == ["meta/vision-test"]


def test_provider_models_shows_the_mixed_catalog_warning(catalog):
    _add_nvidia_via_env()
    result = runner.invoke(app, ["provider", "models", "nvidia-build"])
    assert result.exit_code == 0
    assert "not automatically compatible" in result.stdout
    assert "embedding" in result.stdout


# --------------------------------------------------------------------------- §17.4 probe command


def test_provider_probe_json_output(monkeypatch, catalog):
    _add_nvidia_via_env()

    async def _probe(self, provider_name, model_id, *, refresh=False):  # noqa: ANN001
        return _verified(model_id)

    monkeypatch.setattr(ProviderService, "probe_model", _probe)
    result = runner.invoke(
        app, ["provider", "probe", "nvidia-build", "--model", "nvidia/nemotron-test", "--json"]
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["provider"] == "nvidia-build"
    assert payload["model"] == "nvidia/nemotron-test"
    assert payload["text"] is True
    assert payload["streaming"] is True
    assert payload["tool_calling"] is True
    assert payload["agent_compatible"] is True
    assert payload["tested_at"]
    assert FAKE_KEY not in result.stdout


def test_provider_probe_exits_nonzero_for_a_partial_model(monkeypatch, catalog):
    _add_nvidia_via_env()

    async def _probe(self, provider_name, model_id, *, refresh=False):  # noqa: ANN001
        return _partial(model_id)

    monkeypatch.setattr(ProviderService, "probe_model", _probe)
    result = runner.invoke(app, ["provider", "probe", "nvidia-build", "--model", "nvidia/x"])
    assert result.exit_code == 1
    assert "tool calling was not verified" in result.stdout


# --------------------------------------------------------------------------- §18 provider test honesty


def test_provider_test_never_claims_the_key_is_valid(monkeypatch):
    _add_nvidia_via_env()

    from openagent.providers.base import HealthResult

    async def _test(self, name):  # noqa: ANN001
        return HealthResult(ok=True, detail="reachable")

    monkeypatch.setattr(ProviderService, "test", _test)
    result = runner.invoke(app, ["provider", "test", "nvidia-build"])
    assert result.exit_code == 0, result.stdout
    assert "catalog reachable" in result.stdout
    assert "have not yet been validated" in result.stdout
    # It must never overclaim.
    lowered = result.stdout.lower()
    assert "authenticated" not in lowered
    assert "api key valid" not in lowered


def test_provider_test_with_model_runs_a_real_probe(monkeypatch):
    _add_nvidia_via_env()

    async def _probe(self, provider_name, model_id, *, refresh=False):  # noqa: ANN001
        return _verified(model_id)

    monkeypatch.setattr(ProviderService, "probe_model", _probe)
    result = runner.invoke(
        app, ["provider", "test", "nvidia-build", "--model", "nvidia/nemotron-test"]
    )
    assert result.exit_code == 0, result.stdout
    assert "Verified Agent Compatible" in result.stdout


# --------------------------------------------------------------------------- §17.5 create gate


def test_agent_add_refuses_an_unprobed_mixed_catalog_model(catalog):
    _add_nvidia_via_env()
    result = runner.invoke(
        app,
        [
            "add",
            "--name",
            "nvidia-coder",
            "--provider",
            "nvidia-build",
            "--model",
            "nvidia/nemotron-test",
        ],
    )
    assert result.exit_code == 1
    combined = result.stdout + str(result.stderr or "")
    assert "has not been validated" in combined
    assert "openagent provider probe nvidia-build --model nvidia/nemotron-test" in combined
    assert runner.invoke(app, ["agent", "list", "--json"]).stdout.strip() in ("[]", "[]\n")


def test_agent_add_allows_explicit_unverified_override(catalog):
    _add_nvidia_via_env()
    result = runner.invoke(
        app,
        [
            "add",
            "--name",
            "nvidia-coder",
            "--provider",
            "nvidia-build",
            "--model",
            "nvidia/nemotron-test",
            "--allow-unverified-model",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "NOT verified agent-compatible" in result.stdout  # the override is loudly reported
    agents = json.loads(runner.invoke(app, ["agent", "list", "--json"]).stdout)
    assert [a["name"] for a in agents] == ["nvidia-coder"]


def test_agent_add_succeeds_after_a_verified_probe(monkeypatch, catalog):
    _add_nvidia_via_env()

    async def _probe(self, provider_name, model_id, *, refresh=False):  # noqa: ANN001
        result = _verified(model_id)
        # Mirror the real service: a probe persists the verdict the create gate reads, including
        # from the fresh OpenAgentApp built by the next CLI invocation.
        provider = self.get(provider_name)
        self._store_probe(provider, model_id, result)
        return result

    monkeypatch.setattr(ProviderService, "probe_model", _probe)
    probed = runner.invoke(
        app, ["provider", "probe", "nvidia-build", "--model", "nvidia/nemotron-test"]
    )
    assert probed.exit_code == 0, probed.stdout
    # A fresh CLI invocation builds a new app. The verified result must therefore come from SQLite,
    # not an in-memory cache or an explicit override.
    result = runner.invoke(
        app,
        [
            "add",
            "--name",
            "nvidia-coder",
            "--provider",
            "nvidia-build",
            "--model",
            "nvidia/nemotron-test",
        ],
    )
    assert result.exit_code == 0, result.stdout


def test_non_mixed_catalog_providers_are_not_gated():
    """The probe gate is scoped to mixed catalogs — a normal provider still creates freely."""

    add = runner.invoke(
        app,
        [
            "provider",
            "add",
            "ds",
            "--type",
            "custom",
            "--base-url",
            "https://api.test/v1",
            "--key-env",
            "DS_KEY",
        ],
    )
    assert add.exit_code == 0, add.stdout
    result = runner.invoke(app, ["add", "--name", "ds-coder", "--provider", "ds", "--model", "m"])
    assert result.exit_code == 0, result.stdout
