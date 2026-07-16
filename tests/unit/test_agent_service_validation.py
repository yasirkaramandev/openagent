"""Service-layer boundary: AgentService rejects non-string runtime bindings before Pydantic.

Even if a Textual sentinel (or any non-string) slips past the UI, the service must raise a clean
:class:`AgentError` — never a raw Pydantic ``ValidationError`` from ``AgentRuntime``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Select

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import RuntimeType
from openagent.services.agent_service import AgentError


def _oa(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    return OpenAgentApp(
        Paths(
            data_dir=tmp_path / "data",
            config_dir=tmp_path / "config",
            db_path=tmp_path / "data" / "openagent.db",
            project_root=project,
        )
    )


def test_cli_agent_with_null_sentinel_raises_agent_error(tmp_path: Path):
    oa = _oa(tmp_path)
    # Reproduces the crash input at the service boundary: cli=Select.NULL.
    with pytest.raises(AgentError, match="CLI agent requires a valid CLI selection"):
        oa.agents.create(name="x", runtime_type=RuntimeType.CLI, cli=Select.NULL)  # type: ignore[arg-type]
    assert oa.agents.get("x") is None


def test_cli_agent_with_none_raises_agent_error(tmp_path: Path):
    oa = _oa(tmp_path)
    with pytest.raises(AgentError, match="CLI agent requires a valid CLI selection"):
        oa.agents.create(name="x", runtime_type=RuntimeType.CLI, cli=None)


def test_api_agent_with_null_provider_raises(tmp_path: Path):
    oa = _oa(tmp_path)
    with pytest.raises(AgentError, match="valid provider connection"):
        oa.agents.create(
            name="x", runtime_type=RuntimeType.API_AGENT, provider=Select.NULL, model="m"
        )  # type: ignore[arg-type]


def test_api_agent_with_null_model_raises(tmp_path: Path):
    oa = _oa(tmp_path)
    with pytest.raises(AgentError, match="valid model id"):
        oa.agents.create(
            name="x", runtime_type=RuntimeType.API_AGENT, provider="p", model=Select.NULL
        )  # type: ignore[arg-type]


def test_empty_name_raises(tmp_path: Path):
    oa = _oa(tmp_path)
    with pytest.raises(AgentError, match="agent name is required"):
        oa.agents.create(name="   ", runtime_type=RuntimeType.CLI, cli="codex")


def test_valid_cli_agent_succeeds(tmp_path: Path):
    oa = _oa(tmp_path)
    agent = oa.agents.create(name="ok", runtime_type=RuntimeType.CLI, cli="codex")
    assert agent.runtime.cli == "codex"
    assert agent.runtime.provider is None and agent.runtime.model is None


# --------------------------------------------------------------------------- provider reference (item 7)


def test_api_agent_with_missing_provider_reference_raises(tmp_path: Path):
    oa = _oa(tmp_path)
    with pytest.raises(AgentError, match="provider 'nope' does not exist"):
        oa.agents.create(name="x", runtime_type=RuntimeType.API_AGENT, provider="nope", model="m")
    assert oa.agents.get("x") is None


def test_api_agent_with_existing_provider_reference_succeeds(tmp_path: Path):
    oa = _oa(tmp_path)
    oa.providers.add(name="ds", provider_type="deepseek", key_env="DS_KEY", credential_source="env")
    agent = oa.agents.create(
        name="ok", runtime_type=RuntimeType.API_AGENT, provider="ds", model="deepseek-chat"
    )
    assert agent.runtime.provider == "ds" and agent.runtime.model == "deepseek-chat"
    assert agent.runtime.model_verification is not None
    assert agent.runtime.model_verification.status == "unverified"


def test_model_override_reason_is_persisted_and_never_marked_verified(tmp_path: Path):
    oa = _oa(tmp_path)
    oa.providers.add(name="ds", provider_type="deepseek", key_env="DS_KEY", credential_source="env")
    agent = oa.agents.create(
        name="override",
        runtime_type=RuntimeType.API_AGENT,
        provider="ds",
        model="deepseek-chat",
        model_override_reason="temporary provider outage during probe",
    )
    verification = agent.runtime.model_verification
    assert verification is not None
    assert verification.status == "overridden"
    assert verification.override_reason == "temporary provider outage during probe"
    assert verification.verified_at is None
