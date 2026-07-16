from __future__ import annotations

import json
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.events import EventType, NormalizedEvent
from openagent.core.models import Run
from openagent.services.doctor_service import OK, WARN
from openagent.services.project_service import ProjectError
from openagent.storage.event_log import EventLog


def _paths(root: Path, data: Path) -> Paths:
    return Paths(
        data_dir=data,
        config_dir=data / "config",
        db_path=data / "openagent.db",
        project_root=root,
    )


def test_project_marker_uuid_is_stable_across_processes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = _paths(root, tmp_path / "data")
    first = OpenAgentApp(paths)
    second = OpenAgentApp(paths)
    marker = json.loads((root / ".openagent" / "project.json").read_text())
    assert first.project.id == second.project.id == marker["id"]


def test_project_relocate_updates_runs_and_requires_matching_marker(tmp_path: Path) -> None:
    old = tmp_path / "old"
    old.mkdir()
    app = OpenAgentApp(_paths(old, tmp_path / "data"))
    run = Run(
        id="run_move",
        agent="agent",
        project_id=app.project.id,
        project_root=str(old),
        project_state_dir=str(old / ".openagent"),
        artifact_dir=str(old / ".openagent" / "runs" / "run_move"),
        workspace=str(old),
    )
    app.repos.runs.upsert(run)
    moved = tmp_path / "moved"
    old.rename(moved)

    project = app.projects.relocate(app.project.id, moved)
    stored = app.repos.runs.get(run.id)
    assert project.root == str(moved.resolve())
    assert stored.project_root == str(moved.resolve())
    assert stored.artifact_dir == str(moved / ".openagent" / "runs" / run.id)

    wrong = tmp_path / "wrong"
    wrong.mkdir()
    OpenAgentApp(_paths(wrong, tmp_path / "other-data"))
    with pytest.raises(ProjectError, match="does not match"):
        app.projects.relocate(app.project.id, wrong)


async def test_sqlite_events_survive_corrupt_export_and_repair(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    app = OpenAgentApp(_paths(root, tmp_path / "data"))
    run = Run(
        id="run_events",
        agent="agent",
        project_id=app.project.id,
        project_root=str(root),
        artifact_dir=str(app.paths.run_dir("run_events")),
    )
    app.repos.runs.upsert(run)
    log = EventLog(app.paths.run_dir(run.id), index=app.repos.event_index, run_id=run.id)
    log.append(NormalizedEvent(run_id=run.id, type=EventType.RUN_STARTED, source="openagent"))
    authoritative = app.repos.event_index.read_raw(run.id)
    log.path.write_text("{corrupt\n")

    # Replay is DB-authoritative and doctor identifies only the repairable export mismatch.
    assert [event.id for event in log.read()] == [authoritative[0]["id"]]
    before = {check.name: check for check in await app.doctor.run()}
    assert before["Event store integrity"].status == WARN

    result = app.runs.repair_event_export(run.id)
    assert result["repaired"] is True
    assert EventLog(log.run_dir).read_raw() == authoritative
    after = {check.name: check for check in await app.doctor.run()}
    assert after["Event store integrity"].status == OK
