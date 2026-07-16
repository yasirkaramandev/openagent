from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.lifecycle import (
    InvalidTransition,
    can_cancel,
    can_resume,
    can_transition,
    is_terminal,
    validate_transition,
)
from openagent.core.models import Run, RunStatus


def test_terminal_resume_and_cancel_contract() -> None:
    assert is_terminal(RunStatus.ORPHANED)
    assert is_terminal(RunStatus.CANCELLED)
    assert can_resume(RunStatus.COMPLETED)
    assert can_resume(RunStatus.FAILED)
    assert not can_resume(RunStatus.ORPHANED)
    assert not can_resume(RunStatus.CANCELLED)
    assert can_cancel(RunStatus.RUNNING)
    assert not can_cancel(RunStatus.COMPLETED)


def test_cancelled_and_orphaned_cannot_be_revived() -> None:
    assert not can_transition(RunStatus.CANCELLED, RunStatus.RUNNING)
    assert not can_transition(RunStatus.ORPHANED, RunStatus.RUNNING)


def test_persisted_queued_run_can_be_orphaned_after_restart() -> None:
    assert can_transition(RunStatus.QUEUED, RunStatus.ORPHANED)
    with pytest.raises(InvalidTransition):
        validate_transition(RunStatus.CANCELLED, RunStatus.RUNNING)


def test_repository_transition_is_compare_and_set(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    app = OpenAgentApp(
        Paths(
            data_dir=tmp_path / "data",
            config_dir=tmp_path / "config",
            db_path=tmp_path / "data" / "openagent.db",
            project_root=project,
        )
    )
    run = Run(id="run_cas", agent="agent", status=RunStatus.RUNNING)
    app.repos.runs.upsert(run)

    winner = run.model_copy(update={"status": RunStatus.FAILED})
    loser = run.model_copy(update={"status": RunStatus.CANCELLED})
    assert app.repos.runs.compare_and_set_transition(winner, expected={RunStatus.RUNNING})
    assert not app.repos.runs.compare_and_set_transition(loser, expected={RunStatus.RUNNING})
    assert app.repos.runs.get(run.id).status is RunStatus.FAILED
