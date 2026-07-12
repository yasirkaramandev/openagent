"""Provider deletion is refused while agents still bind to it (item 4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import RuntimeType
from openagent.services.provider_service import ProviderInUseError


def _app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    paths = Paths(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db", project_root=project,
    )
    return OpenAgentApp(paths)


def test_remove_refused_when_agent_uses_provider(tmp_path: Path) -> None:
    oa = _app(tmp_path)
    oa.providers.add(name="deepseek-main", provider_type="deepseek", api_key="sk-x", store_key=False)
    oa.agents.create(name="ds-coder", runtime_type=RuntimeType.API_AGENT,
                     provider="deepseek-main", model="deepseek-chat")
    oa.agents.create(name="backend-reviewer", runtime_type=RuntimeType.API_AGENT,
                     provider="deepseek-main", model="deepseek-chat")

    with pytest.raises(ProviderInUseError) as exc:
        oa.providers.remove("deepseek-main")

    # Error names every dependent agent, and the provider is still there.
    assert set(exc.value.agents) == {"ds-coder", "backend-reviewer"}
    assert "ds-coder" in str(exc.value) and "backend-reviewer" in str(exc.value)
    assert oa.providers.get("deepseek-main") is not None


def test_remove_succeeds_after_dependents_gone(tmp_path: Path) -> None:
    oa = _app(tmp_path)
    oa.providers.add(name="deepseek-main", provider_type="deepseek", api_key="sk-x", store_key=False)
    oa.agents.create(name="ds-coder", runtime_type=RuntimeType.API_AGENT,
                     provider="deepseek-main", model="deepseek-chat")
    oa.agents.remove("ds-coder")
    assert oa.providers.remove("deepseek-main") is True
    assert oa.providers.get("deepseek-main") is None


def test_remove_missing_provider_returns_false(tmp_path: Path) -> None:
    oa = _app(tmp_path)
    assert oa.providers.remove("nope") is False
