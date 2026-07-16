from pathlib import Path

from openagent.core.events import EventType, NormalizedEvent
from openagent.core.models import (
    AgentProfile,
    AgentRuntime,
    Protocol,
    ProviderConnection,
    Run,
    RunStatus,
    RuntimeType,
)
from openagent.storage.event_log import EventLog
from openagent.storage.repositories import Repositories


def test_provider_roundtrip(repos: Repositories):
    provider = ProviderConnection(
        id="provider_deepseek_main",
        name="deepseek-main",
        provider_type="deepseek",
        protocol=Protocol.OPENAI_CHAT,
        base_url="https://api.deepseek.com",
    )
    repos.providers.upsert(provider)
    assert repos.providers.get("provider_deepseek_main").name == "deepseek-main"
    assert repos.providers.get_by_name("deepseek-main").provider_type == "deepseek"
    assert len(repos.providers.list()) == 1
    repos.providers.delete("provider_deepseek_main")
    assert repos.providers.get("provider_deepseek_main") is None


def test_agent_roundtrip(repos: Repositories):
    agent = AgentProfile(
        name="deepseek-coder",
        title="DeepSeek Coder",
        runtime=AgentRuntime(type=RuntimeType.API_AGENT, provider="deepseek-main", model="m"),
        tags=["coder", "python"],
        permission_profile="safe-edit",
    )
    repos.agents.upsert(agent)
    loaded = repos.agents.get("deepseek-coder")
    assert loaded.runtime.type == RuntimeType.API_AGENT
    assert loaded.tags == ["coder", "python"]
    assert repos.agents.delete("deepseek-coder") is True
    assert repos.agents.delete("deepseek-coder") is False


def test_run_status_update(repos: Repositories):
    run = Run(id="run_01ABC", agent="codex-coder", workspace="/tmp/x")
    repos.runs.upsert(run)
    assert repos.runs.get("run_01ABC").status == RunStatus.QUEUED
    assert len(repos.runs.list_active()) == 1
    run.status = RunStatus.COMPLETED
    repos.runs.upsert(run)
    assert repos.runs.get("run_01ABC").status == RunStatus.COMPLETED
    assert repos.runs.list_active() == []


def test_event_log_writes_and_indexes(tmp_path: Path, repos: Repositories):
    run_dir = tmp_path / "run_01ABC"
    log = EventLog(run_dir, index=repos.event_index)
    log.append(NormalizedEvent(run_id="run_01ABC", type=EventType.RUN_STARTED, source="openagent"))
    log.append(
        NormalizedEvent(run_id="run_01ABC", type=EventType.RUN_COMPLETED, source="openagent")
    )
    events = list(log.read())
    assert [e.type for e in events] == ["run.started", "run.completed"]
    assert repos.event_index.count("run_01ABC") == 2
    assert repos.event_index.next_seq("run_01ABC") == 3


def test_event_log_redacts_secrets(tmp_path: Path):
    log = EventLog(tmp_path / "run_x")
    log.append(
        NormalizedEvent(
            run_id="run_x",
            type=EventType.LOG,
            source="api-agent",
            data={"line": "exported OPENAI_API_KEY=sk-abcdEFGH1234567890zzzz"},
        )
    )
    body = (tmp_path / "run_x" / "events.jsonl").read_text()
    assert "sk-abcdEFGH1234567890zzzz" not in body
    assert "REDACTED" in body
