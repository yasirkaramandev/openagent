"""Run lifecycle: one run.started, phases, preflight gating, in-place confirmation (items 4, 7, 8, 22).

The old pipeline emitted ``run.started`` twice — once from OpenAgent and once from whichever CLI
adapter was driving, carrying the pid — so a reader could not tell how many runs had begun, and the
pid rode in on an event that did not mean "a process started". Failures, meanwhile, were only ever
warnings in an in-memory bundle, so ``events.jsonl`` never recorded why a run died.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import RuntimeType
from openagent.services.run_service import RunError
from tests.fakecli import FakeCliAdapter, install_fake_cli, write_fake_script


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture()
def oa(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    _git(["init", "-q"], project)
    _git(["config", "user.email", "t@t.com"], project)
    _git(["config", "user.name", "t"], project)
    (project / "seed.txt").write_text("seed\n")
    _git(["add", "-A"], project)
    _git(["commit", "-q", "-m", "init"], project)
    app = OpenAgentApp(
        Paths(
            data_dir=tmp_path / "data",
            config_dir=tmp_path / "config",
            db_path=tmp_path / "data" / "openagent.db",
            project_root=project,
        )
    )
    app.agents.create(name="fake-coder", runtime_type=RuntimeType.CLI, cli="fake")
    return app


@pytest.fixture()
def use_fake(oa: OpenAgentApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCliAdapter:
    return install_fake_cli(monkeypatch, FakeCliAdapter(write_fake_script(tmp_path)))


def _events(oa: OpenAgentApp, run_id: str) -> list[dict]:
    return [
        json.loads(line) for line in oa.runs.output(run_id, "events").splitlines() if line.strip()
    ]


def _types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


# --------------------------------------------------------------------------- one run.started (item 4)


async def test_exactly_one_run_started_and_one_terminal_event(oa: OpenAgentApp, use_fake):
    run = oa.runs.create(agent_name="fake-coder", prompt="go", worktree="auto")
    await oa.runs.execute(run)

    events = _events(oa, run.id)
    types = _types(events)

    assert types.count("run.started") == 1, "a backend adapter emitted a second run.started"
    terminal = [t for t in types if t in ("run.completed", "run.failed", "run.cancelled")]
    assert terminal == ["run.completed"]

    # run.started is OpenAgent's own semantic event and carries no pid…
    started = next(e for e in events if e["type"] == "run.started")
    assert started["source"] == "openagent"
    assert "pid" not in started["data"]

    # …the pid arrives on process.started, which is the fact that a backend process came up.
    process = next(e for e in events if e["type"] == "process.started")
    assert process["source"] == "fake-cli"
    assert process["data"]["pid"] > 0
    assert oa.runs.get(run.id).pid == process["data"]["pid"]


async def test_phases_are_reported_in_order(oa: OpenAgentApp, use_fake):
    run = oa.runs.create(agent_name="fake-coder", prompt="go", worktree="auto")
    await oa.runs.execute(run)

    phases = [e["data"]["phase"] for e in _events(oa, run.id) if e["type"] == "run.phase"]
    assert phases[:5] == [
        "preflight",
        "preparing_workspace",
        "starting_backend",
        "running",
        "finalizing",
    ]
    assert oa.runs.get(run.id).phase == "completed"


# ------------------------------------------------------- terminal event is the last log entry (item 1)


async def test_terminal_event_is_the_last_log_entry_when_completed(oa: OpenAgentApp, use_fake):
    """A CLI backend emits its own terminal event mid-stream; RunService must still write it LAST.

    Regression: the ``finalizing`` phase + diff used to be logged *after* the backend's
    ``run.completed``, leaving ``events[-1] == run.phase`` and a projection that read
    "status: completed / phase: finalizing" — the state the TUI must never show (item 1).
    """

    run = oa.runs.create(agent_name="fake-coder", prompt="go", worktree="auto")
    await oa.runs.execute(run)

    events = _events(oa, run.id)
    assert events[-1]["type"] == "run.completed", "finalizing must not trail the terminal event"
    assert _types(events).count("run.completed") == 1
    # …and the replayed projection settles on completed, never finalizing.
    proj = oa.runs.projection(run.id)
    assert proj.status == "completed"
    assert proj.phase == "completed"
    assert oa.runs.get(run.id).phase == "completed"


async def test_terminal_event_is_the_last_log_entry_when_failed(
    oa: OpenAgentApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_fake_cli(monkeypatch, FakeCliAdapter(write_fake_script(tmp_path), mode="fail1"))
    run = oa.runs.create(agent_name="fake-coder", prompt="go", worktree="auto")
    await oa.runs.execute(run)

    events = _events(oa, run.id)
    assert events[-1]["type"] == "run.failed"
    proj = oa.runs.projection(run.id)
    assert proj.status == "failed"
    assert proj.phase == "failed"
    assert oa.runs.get(run.id).phase == "failed"


# --------------------------------------------------------------------------- preflight gates (item 7)


async def test_preflight_failure_prevents_execution(
    oa: OpenAgentApp, monkeypatch: pytest.MonkeyPatch
):
    """A missing CLI blocks the run — and the failure is *recorded*, not just warned about."""

    # The agent points at "fake", which is not registered: preflight cannot resolve it.
    run = oa.runs.create(agent_name="fake-coder", prompt="go", worktree="auto")
    result = await oa.runs.execute(run)

    assert result.status.value == "failed"
    assert result.failure_type == "cli_not_found"

    events = _events(oa, run.id)
    failure = next(e for e in events if e["type"] == "run.failed")
    assert failure["data"]["error_type"] == "cli_not_found"
    assert failure["data"]["phase"] == "preflight"
    assert failure["data"]["source"] == "openagent"

    # It never reached the backend: no process, no workspace preparation.
    assert "process.started" not in _types(events)
    assert result.pid is None

    # And the failure is visible in every artifact a user or another agent would read (item 13):
    # events.jsonl (above), status.json, result.json and output.md.
    status = json.loads(oa.runs.output(run.id, "status"))
    result_json = json.loads(oa.runs.output(run.id, "json"))
    assert status["failure_type"] == "cli_not_found"
    assert result_json["failure_type"] == "cli_not_found"
    assert result_json["status"] == "failed"

    output_md = oa.runs.output(run.id, "md")
    assert "cli_not_found" in output_md
    assert "preflight" in output_md
    assert "not a known CLI" in output_md  # the safe, actionable message — no secrets


async def test_api_agent_with_missing_credential_is_blocked(tmp_path: Path):
    app = OpenAgentApp(
        Paths(
            data_dir=tmp_path / "d",
            config_dir=tmp_path / "c",
            db_path=tmp_path / "d" / "o.db",
            project_root=tmp_path,
        )
    )
    # A provider whose keychain secret was never stored: locally unconfigured, so it cannot run.
    app.providers.add(
        name="testco",
        provider_type="custom",
        base_url="https://api.test/v1",
        api_key="sk-x",
        store_key=False,
    )
    app.agents.create(
        name="api-coder", runtime_type=RuntimeType.API_AGENT, provider="testco", model="m"
    )

    run = app.runs.create(
        agent_name="api-coder", prompt="go", worktree="none", permission_profile="read-only"
    )
    result = await app.runs.execute(run)

    assert result.status.value == "failed"
    assert result.failure_type == "credential_missing"
    assert "message.started" not in _types(_events(app, run.id)), "the provider was contacted"


# --------------------------------------------------------------------------- in-place confirmation (item 8)


def test_in_place_editing_run_requires_explicit_confirmation(oa: OpenAgentApp):
    """`worktree=none` + an editing profile must not start without explicit consent (item 8)."""

    with pytest.raises(RunError, match="confirmation"):
        oa.runs.create(
            agent_name="fake-coder", prompt="go", worktree="none", permission_profile="safe-edit"
        )

    # Read-only in place is fine — nothing can be edited.
    ok = oa.runs.create(
        agent_name="fake-coder", prompt="go", worktree="none", permission_profile="read-only"
    )
    assert ok.worktree_strategy == "none"

    # …and with explicit confirmation the editing run is allowed.
    confirmed = oa.runs.create(
        agent_name="fake-coder",
        prompt="go",
        worktree="none",
        permission_profile="safe-edit",
        confirm_in_place=True,
    )
    assert confirmed.worktree_strategy == "none"
