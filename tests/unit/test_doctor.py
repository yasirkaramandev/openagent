"""Doctor reports credential/agent/provider health offline (item 20)."""

from __future__ import annotations

from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.events import EventType, NormalizedEvent
from openagent.core.models import (
    AgentProfile,
    AgentRuntime,
    CredentialRef,
    CredentialType,
    Protocol,
    ProviderConnection,
    Run,
    RunStatus,
    RuntimeType,
)
from openagent.services.doctor_service import FAIL, OK, WARN
from openagent.storage.event_log import EventLog


def _app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    paths = Paths(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db",
        project_root=project,
    )
    return OpenAgentApp(paths)


def _checks_by_name(checks) -> dict[str, object]:
    return {c.name: c for c in checks}


async def test_keychain_provider_missing_key_is_fail(tmp_path: Path):
    oa = _app(tmp_path)
    # Directly persist a key-required provider whose keychain has no secret (bypassing validation).
    oa.repos.providers.upsert(
        ProviderConnection(
            id="provider_ds",
            name="ds",
            provider_type="deepseek",
            protocol=Protocol.OPENAI_CHAT,
            credential=CredentialRef(type=CredentialType.KEYCHAIN, account="provider/ds"),
        )
    )
    checks = _checks_by_name(await oa.doctor.run())
    assert checks["Credential: ds"].status == FAIL


async def test_env_credential_var_unset_is_warn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    oa = _app(tmp_path)
    monkeypatch.delenv("DS_UNSET_KEY", raising=False)
    oa.providers.add(
        name="ds", provider_type="deepseek", key_env="DS_UNSET_KEY", credential_source="env"
    )
    checks = _checks_by_name(await oa.doctor.run())
    assert checks["Credential: ds"].status == WARN
    assert "not set" in checks["Credential: ds"].detail


async def test_agent_missing_provider_is_fail(tmp_path: Path):
    oa = _app(tmp_path)
    # A *legacy* broken record: written straight to the repo, bypassing service validation (which
    # now rejects a dangling provider at creation — item 7). Doctor must still flag it (item 20).
    oa.repos.agents.upsert(
        AgentProfile(
            name="ghost-agent",
            runtime=AgentRuntime(type=RuntimeType.API_AGENT, provider="does-not-exist", model="m"),
        )
    )
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
    oa.providers.add(
        name="ds", provider_type="deepseek", key_env="DS_SET_KEY", credential_source="env"
    )
    checks = _checks_by_name(await oa.doctor.run())
    assert checks["Credential: ds"].status not in (FAIL, WARN)


async def test_doctor_reports_all_cli_runtimes(tmp_path: Path):
    """Doctor reports every known CLI runtime — including Antigravity — with an install line and,
    when installed, an adapter-status line that never claims readiness from a mere binary (item 18)."""
    oa = _app(tmp_path)
    names = {c.name for c in await oa.doctor.run()}
    for cli in ("Codex CLI", "Claude Code", "Antigravity"):
        assert f"{cli} installed" in names


async def test_doctor_antigravity_status_line_when_installed(tmp_path: Path):
    """When Antigravity is installed, the adapter-status line distinguishes structured output +
    resume support from mere detection (item 18). Skips cleanly when agy is absent on this host."""
    oa = _app(tmp_path)
    checks = _checks_by_name(await oa.doctor.run())
    if "Antigravity installed" in checks and checks["Antigravity installed"].status == OK:
        status = checks["Antigravity adapter status"]
        assert "structured output: yes" in status.detail
        assert "resume: yes" in status.detail


def test_doctor_accepts_orphaned_then_cancelled_terminal_chain(tmp_path: Path) -> None:
    oa = _app(tmp_path)
    run = Run(id="run_chain", agent="ghost", status=RunStatus.CANCELLED)
    oa.repos.runs.upsert(run)
    log = EventLog(oa.paths.run_dir(run.id), index=oa.repos.event_index, run_id=run.id)
    log.append(NormalizedEvent(run_id=run.id, type=EventType.RUN_ORPHANED, source="test"))
    log.append(NormalizedEvent(run_id=run.id, type=EventType.RUN_CANCELLED, source="test"))

    check = oa.doctor._event_store_check()
    assert check.status != FAIL, check.detail


def test_doctor_rejects_conflicting_terminal_chain(tmp_path: Path) -> None:
    oa = _app(tmp_path)
    run = Run(id="run_bad_chain", agent="ghost", status=RunStatus.FAILED)
    oa.repos.runs.upsert(run)
    log = EventLog(oa.paths.run_dir(run.id), index=oa.repos.event_index, run_id=run.id)
    log.append(NormalizedEvent(run_id=run.id, type=EventType.RUN_COMPLETED, source="test"))
    log.append(NormalizedEvent(run_id=run.id, type=EventType.RUN_FAILED, source="test"))

    check = oa.doctor._event_store_check()
    assert check.status == FAIL
    assert "invalid terminal chain" in check.detail
