from __future__ import annotations

import pytest

from app.models import Priority, ValidationError
from app.repository import TaskNotFoundError, TaskRepository
from app.service import TaskBoardService


def service_for(tmp_path) -> TaskBoardService:
    return TaskBoardService(TaskRepository(tmp_path / "board.json"))


def test_service_adds_lists_and_summarizes_tasks(tmp_path) -> None:
    service = service_for(tmp_path)
    service.add("Write the reference project", "high")
    second = service.add("Verify it offline", Priority.MEDIUM)
    service.complete(second.id)

    assert [task.title for task in service.list()] == [
        "Write the reference project",
        "Verify it offline",
    ]
    assert service.summary() == {"total": 2, "completed": 1, "pending": 1}


@pytest.mark.parametrize(
    ("title", "priority", "message"),
    [
        ("", "low", "title"),
        ("ok", "critical", "priority"),
        ("line\nbreak", "medium", "control"),
    ],
)
def test_service_validates_untrusted_input(tmp_path, title, priority, message) -> None:
    with pytest.raises(ValidationError, match=message):
        service_for(tmp_path).add(title, priority)


def test_service_reports_missing_task(tmp_path) -> None:
    with pytest.raises(TaskNotFoundError, match="7"):
        service_for(tmp_path).complete("7")
