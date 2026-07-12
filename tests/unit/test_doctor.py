"""Doctor reports credential/agent/provider health offline (item 20)."""

from __future__ import annotations

from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import (
    CredentialRef,
    CredentialType,
    Protocol,
    ProviderConnection,
    RuntimeType,
)
from openagent.services.doctor_service import FAIL, WARN


def _app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    paths = Paths(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db", project_root=project,
    )
    return OpenAgentApp(paths)


def _checks_by_name(checks) -> dict[str, object]:
    return {c.name: c for c in checks}


async def test_keychain_provider_missing_key_is_fail(tmp_path: Path):
    oa = _app(tmp_path)
    # Directly persist a key-required provider whose keychain has no secret (bypassing validation).
    oa.repos.providers.upsert(ProviderConnection(
        id="provider_ds", name="ds", provider_type="deepseek", protocol=Protocol.OPENAI_CHAT,
        credential=CredentialRef(type=CredentialType.KEYCHAIN, account="provider/ds"),
    ))
    checks = _checks_by_name(await oa.doctor.run())
    assert checks["Credential: ds"].status == FAIL


async def test_env_credential_var_unset_is_warn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    oa = _app(tmp_path)
    monkeypatch.delenv("DS_UNSET_KEY", raising=False)
    oa.providers.add(name="ds", provider_type="deepseek", key_env="DS_UNSET_KEY",
                     credential_source="env")
    checks = _checks_by_name(await oa.doctor.run())
    assert checks["Credential: ds"].status == WARN
    assert "not set" in checks["Credential: ds"].detail


async def test_agent_missing_provider_is_fail(tmp_path: Path):
    oa = _app(tmp_path)
    oa.agents.create(name="ghost-agent", runtime_type=RuntimeType.API_AGENT,
                     provider="does-not-exist", model="m")
    checks = _checks_by_name(await oa.doctor.run())
    assert checks["Agent: ghost-agent"].status == FAIL
    assert "missing provider" in checks["Agent: ghost-agent"].detail


async def test_cli_agent_uninstalled_or_unknown_is_warn(tmp_path: Path):
    oa = _app(tmp_path)
    oa.agents.create(name="mystery-agent", runtime_type=RuntimeType.CLI, cli="ghostcli")
    checks = _checks_by_name(await oa.doctor.run())
    # An unknown CLI runtime is always flagged, regardless of what's installed on the host.
    assert checks["Agent: mystery-agent"].status == WARN
    assert "unknown" in checks["Agent: mystery-agent"].detail


async def test_env_credential_var_set_is_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    oa = _app(tmp_path)
    monkeypatch.setenv("DS_SET_KEY", "sk-x")
    oa.providers.add(name="ds", provider_type="deepseek", key_env="DS_SET_KEY",
                     credential_source="env")
    checks = _checks_by_name(await oa.doctor.run())
    assert checks["Credential: ds"].status not in (FAIL, WARN)
