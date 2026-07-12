"""Atomic provider+agent creation and rollback (item 3).

`AgentService.create_with_new_provider` must leave the system exactly as it started whenever agent
creation fails after the provider was written: no provider row, no keychain secret, no half-written
OPENAGENT.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import RuntimeType
from openagent.credentials.store import CredentialError
from openagent.services.agent_service import AgentError


def _app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    paths = Paths(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db", project_root=project,
    )
    return OpenAgentApp(paths)


def _md_missing_agent(oa: OpenAgentApp, agent: str) -> bool:
    md = oa.paths.openagent_md()
    return (not md.exists()) or (f"`{agent}`" not in md.read_text(encoding="utf-8"))


def test_happy_path_creates_both(tmp_path: Path) -> None:
    oa = _app(tmp_path)
    agent = oa.agents.create_with_new_provider(
        provider_name="ds", provider_type="custom", base_url="https://api.test/v1",
        key_env="DS_KEY", credential_source="env", model="m", name="ds-coder",
    )
    assert agent.name == "ds-coder"
    assert oa.providers.get("ds") is not None
    assert not _md_missing_agent(oa, "ds-coder")


def test_duplicate_agent_name_leaves_no_provider(tmp_path: Path) -> None:
    oa = _app(tmp_path)
    oa.agents.create(name="dup", runtime_type=RuntimeType.CLI, cli="codex")
    with pytest.raises(AgentError):
        oa.agents.create_with_new_provider(
            provider_name="ds", provider_type="custom", base_url="https://api.test/v1",
            key_env="DS_KEY", credential_source="env", model="m", name="dup",
        )
    assert oa.providers.get("ds") is None


def test_agent_validation_failure_leaves_no_provider(tmp_path: Path) -> None:
    oa = _app(tmp_path)
    with pytest.raises(AgentError):
        oa.agents.create_with_new_provider(
            provider_name="ds", provider_type="custom", base_url="https://api.test/v1",
            key_env="DS_KEY", credential_source="env", model="", name="ds-coder",
        )
    assert oa.providers.get("ds") is None


def test_openagent_md_write_failure_rolls_back_provider_and_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oa = _app(tmp_path)

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("openagent.services.agent_service.write_openagent_md", boom)
    with pytest.raises(OSError):
        oa.agents.create_with_new_provider(
            provider_name="ds", provider_type="custom", base_url="https://api.test/v1",
            key_env="DS_KEY", credential_source="env", model="m", name="ds-coder",
        )
    # Provider row rolled back, agent row rolled back.
    assert oa.providers.get("ds") is None
    assert oa.agents.get("ds-coder") is None


def test_keychain_write_failure_persists_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oa = _app(tmp_path)

    def boom(*_a: object, **_k: object) -> None:
        raise CredentialError("keyring unavailable")

    monkeypatch.setattr(oa.credentials, "set_secret", boom)
    with pytest.raises(CredentialError):
        oa.agents.create_with_new_provider(
            provider_name="ds", provider_type="custom", base_url="https://api.test/v1",
            api_key="sk-secret", credential_source="keychain", model="m", name="ds-coder",
        )
    assert oa.providers.get("ds") is None
    assert oa.agents.get("ds-coder") is None


def test_provider_repository_failure_persists_no_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oa = _app(tmp_path)

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("db is locked")

    monkeypatch.setattr(oa.repos.providers, "upsert", boom)
    with pytest.raises(RuntimeError):
        oa.agents.create_with_new_provider(
            provider_name="ds", provider_type="custom", base_url="https://api.test/v1",
            key_env="DS_KEY", credential_source="env", model="m", name="ds-coder",
        )
    assert oa.providers.get("ds") is None
    assert oa.agents.get("ds-coder") is None
