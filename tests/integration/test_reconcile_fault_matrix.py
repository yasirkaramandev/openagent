"""Fault injection for terminal reconciliation artifact/event durability boundaries."""

from __future__ import annotations

import errno
import hashlib
import json
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.events import EventType, NormalizedEvent
from openagent.core.models import Run, RunStatus
from openagent.reporting.artifacts import ArtifactWriter
from openagent.services.run_service import TerminalEventAppendError, TerminalEventExportError
from openagent.storage.event_log import EventLog


@pytest.fixture()
def app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "project"
    project.mkdir()
    return OpenAgentApp(
        Paths(
            data_dir=tmp_path / "data",
            config_dir=tmp_path / "config",
            db_path=tmp_path / "data" / "openagent.db",
            project_root=project,
        )
    )


def _running(app: OpenAgentApp, run_id: str = "run_reconcile_fault") -> Run:
    run = Run(
        id=run_id,
        agent="ghost",
        status=RunStatus.RUNNING,
        project_id=app.runs.project_id,
        project_root=str(app.paths.project_root),
        artifact_dir=str(app.paths.run_dir(run_id)),
    )
    app.repos.runs.upsert(run)
    EventLog(app.paths.run_dir(run_id), index=app.repos.event_index, run_id=run_id).append(
        NormalizedEvent(run_id=run_id, type=EventType.RUN_STARTED, source="test")
    )
    return run


def _boom(message: str, exception_type: type[Exception] = OSError):
    def raise_error(*_args: object, **_kwargs: object) -> None:
        raise exception_type(message)

    return raise_error


@pytest.mark.parametrize(
    ("method", "failure"),
    [
        ("write_status", OSError("status write failed")),
        ("write_results", OSError("result write failed")),
        ("write_results", OSError(errno.ENOSPC, "disk full")),
        ("write_results", PermissionError(errno.EACCES, "permission denied")),
    ],
)
def test_mandatory_artifact_failure_invalidates_completed_outcome(
    app: OpenAgentApp,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    failure: Exception,
) -> None:
    run = _running(app)
    monkeypatch.setattr(ArtifactWriter, method, _boom(str(failure), type(failure)))

    assert app.runs.reconcile_terminal_bundle(
        run,
        target_status=RunStatus.COMPLETED,
        expected={RunStatus.RUNNING},
    )

    stored = app.repos.runs.get(run.id)
    assert stored is not None and stored.status is RunStatus.FAILED
    terminals = app.repos.event_index.terminal_types(run.id)
    assert terminals == ["run.failed"]
    assert "run.completed" not in terminals
    result_path = app.paths.run_dir(run.id) / "result.json"
    if result_path.exists():
        assert json.loads(result_path.read_text())["status"] == "failed"


@pytest.mark.parametrize("method", ["write_timeline", "write_integrity"])
def test_expected_artifact_failure_marks_partial_without_false_bundle_claim(
    app: OpenAgentApp,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    run = _running(app, f"run_{method}")
    monkeypatch.setattr(ArtifactWriter, method, _boom(f"{method} failed"))

    assert app.runs.reconcile_terminal_bundle(
        run,
        target_status=RunStatus.COMPLETED,
        expected={RunStatus.RUNNING},
    )
    status = json.loads((app.paths.run_dir(run.id) / "status.json").read_text())
    result = json.loads((app.paths.run_dir(run.id) / "result.json").read_text())
    assert status["status"] == result["status"] == "completed"
    assert status["artifacts_partial"] is True
    assert result["artifacts_partial"] is True
    assert app.repos.event_index.terminal_types(run.id) == ["run.completed"]


def test_terminal_event_append_failure_is_not_misclassified_as_artifact_failure(
    app: OpenAgentApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _running(app)
    original = EventLog.append

    def fail_terminal(self: EventLog, event: NormalizedEvent) -> NormalizedEvent:
        if event.type in {
            EventType.RUN_COMPLETED,
            EventType.RUN_FAILED,
            EventType.RUN_CANCELLED,
            EventType.RUN_ORPHANED,
        }:
            raise OSError("SQLite append failed")
        return original(self, event)

    monkeypatch.setattr(EventLog, "append", fail_terminal)
    with pytest.raises(TerminalEventAppendError):
        app.runs.reconcile_terminal_bundle(
            run,
            target_status=RunStatus.COMPLETED,
            expected={RunStatus.RUNNING},
        )

    stored = app.repos.runs.get(run.id)
    assert stored is not None and stored.status is RunStatus.FAILED
    assert stored.failure_type == "terminal_event_append_failed"
    assert app.repos.event_index.terminal_types(run.id) == []


def test_event_export_failure_preserves_authoritative_terminal_event(
    app: OpenAgentApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _running(app)
    monkeypatch.setattr(EventLog, "export", _boom("export failed"))

    with pytest.raises(TerminalEventExportError):
        app.runs.reconcile_terminal_bundle(
            run,
            target_status=RunStatus.COMPLETED,
            expected={RunStatus.RUNNING},
        )

    stored = app.repos.runs.get(run.id)
    assert stored is not None and stored.status is RunStatus.COMPLETED
    assert app.repos.event_index.terminal_types(run.id) == ["run.completed"]
    assert json.loads((app.paths.run_dir(run.id) / "result.json").read_text())["status"] == (
        "completed"
    )


def test_completed_event_implies_parseable_result_and_matching_integrity(app: OpenAgentApp) -> None:
    run = _running(app)
    assert app.runs.reconcile_terminal_bundle(
        run,
        target_status=RunStatus.COMPLETED,
        expected={RunStatus.RUNNING},
    )
    assert app.repos.event_index.terminal_types(run.id) == ["run.completed"]

    run_dir = app.paths.run_dir(run.id)
    json.loads((run_dir / "result.json").read_text())
    manifest = json.loads((run_dir / "integrity.json").read_text())
    for relative, expected_hash in manifest["files"].items():
        assert hashlib.sha256((run_dir / relative).read_bytes()).hexdigest() == expected_hash


def test_terminal_cas_loser_writes_no_artifact_or_event(app: OpenAgentApp) -> None:
    seeded = _running(app)
    winner = app.repos.runs.get(seeded.id)
    loser = app.repos.runs.get(seeded.id)
    assert winner is not None and loser is not None

    assert app.runs.reconcile_terminal_bundle(
        winner,
        target_status=RunStatus.COMPLETED,
        expected={RunStatus.RUNNING},
    )
    before_result = (app.paths.run_dir(seeded.id) / "result.json").read_bytes()
    before_events = app.repos.event_index.read_raw(seeded.id)

    assert not app.runs.reconcile_terminal_bundle(
        loser,
        target_status=RunStatus.FAILED,
        expected={RunStatus.RUNNING},
        failure_type="stale_writer",
    )
    assert (app.paths.run_dir(seeded.id) / "result.json").read_bytes() == before_result
    assert app.repos.event_index.read_raw(seeded.id) == before_events
    assert app.repos.runs.get(seeded.id).status is RunStatus.COMPLETED
