"""The run lifecycle survives setup/finalize failures (item 9.4).

Whatever fails — writing request.json, computing the diff, appending the terminal event, or writing
status/result/timeline — the run must reach a **terminal** state (never left "running"), and an
artifact-write failure must **never** be reported as success. These inject each failure and assert
the guarantee holds.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import RuntimeType, enum_value
from openagent.reporting.artifacts import ArtifactWriter
from openagent.storage.event_log import EventLog
from openagent.workspaces.worktree import WorktreeManager
from tests.fakecli import FakeCliAdapter, install_fake_cli, write_fake_script

_TERMINALS = {"run.completed", "run.failed", "run.cancelled"}


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture()
def oa(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OpenAgentApp:
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
    install_fake_cli(monkeypatch, FakeCliAdapter(write_fake_script(tmp_path)))
    return app


def _boom(*_a: object, **_k: object) -> None:
    raise OSError("injected artifact failure")


async def _run(oa: OpenAgentApp):
    run = oa.runs.create(agent_name="fake-coder", prompt="go", worktree="auto")
    return await oa.runs.execute(run)


def _terminals_in_log(oa: OpenAgentApp, run_id: str) -> list[str]:
    events = [
        json.loads(line) for line in oa.runs.output(run_id, "events").splitlines() if line.strip()
    ]
    return [e["type"] for e in events if e["type"] in _TERMINALS]


async def test_write_request_failure_still_terminates(oa: OpenAgentApp, monkeypatch):
    monkeypatch.setattr(ArtifactWriter, "write_request", _boom)
    result = await _run(oa)
    assert enum_value(result.status) == "failed"
    assert enum_value(oa.runs.get(result.id).status) == "failed"  # not left queued/running


async def test_diff_failure_is_recorded_as_a_terminal_failure(oa: OpenAgentApp, monkeypatch):
    monkeypatch.setattr(WorktreeManager, "diff", _boom)
    result = await _run(oa)
    assert enum_value(result.status) == "failed"
    assert result.failure_type == "finalization_failed"
    # The failure is written, not just left in memory, and there is exactly one terminal event.
    assert json.loads(oa.runs.output(result.id, "status"))["status"] == "failed"
    assert _terminals_in_log(oa, result.id) == ["run.failed"]


async def test_terminal_append_failure_still_terminates(oa: OpenAgentApp, monkeypatch):
    original = EventLog.append

    def append(self, event):
        etype = event.type if isinstance(event.type, str) else event.type.value
        if etype in _TERMINALS:
            raise OSError("injected append failure on terminal event")
        return original(self, event)

    monkeypatch.setattr(EventLog, "append", append)
    result = await _run(oa)
    # The DB is authoritative: even though the terminal event could not be appended, the run is
    # terminal and its recorded status is not "completed".
    assert enum_value(oa.runs.get(result.id).status) == "failed"

    # §5: the whole bundle is reconciled — nothing is left claiming the run completed.
    status = json.loads(oa.runs.output(result.id, "status"))
    result_json = json.loads(oa.runs.output(result.id, "json"))
    assert status["status"] == "failed"
    assert result_json["status"] == "failed"
    # A stale "completed" timeline must not survive.
    timeline = (oa.paths.run_dir(result.id) / "timeline.md").read_text()
    status_line = next(ln for ln in timeline.splitlines() if ln.startswith("- Status:"))
    assert "completed" not in status_line and "failed" in status_line
    # events.jsonl carries no success terminal.
    assert "run.completed" not in oa.runs.output(result.id, "events")
    # The partial bundle is explicitly flagged, with the failing stage.
    assert status.get("artifacts_partial") is True
    assert result_json.get("artifacts_partial") is True
    assert result_json.get("artifact_failure", {}).get("stage") == "finalize"


async def test_write_status_failure_still_terminates(oa: OpenAgentApp, monkeypatch):
    monkeypatch.setattr(ArtifactWriter, "write_status", _boom)
    result = await _run(oa)
    assert enum_value(oa.runs.get(result.id).status) == "failed"


async def test_write_results_failure_is_not_reported_as_success(oa: OpenAgentApp, monkeypatch):
    monkeypatch.setattr(ArtifactWriter, "write_results", _boom)
    result = await _run(oa)
    assert enum_value(result.status) == "failed"
    # status.json is rewritten by the failure path, so the run never *looks* completed.
    assert json.loads(oa.runs.output(result.id, "status"))["status"] == "failed"


async def test_write_timeline_failure_still_terminates(oa: OpenAgentApp, monkeypatch):
    monkeypatch.setattr(ArtifactWriter, "write_timeline", _boom)
    result = await _run(oa)
    assert enum_value(oa.runs.get(result.id).status) == "failed"
