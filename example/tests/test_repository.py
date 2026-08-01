from __future__ import annotations

import json

import pytest

from app.models import Priority, ValidationError
from app.repository import RepositoryError, TaskNotFoundError, TaskRepository


def test_tasks_are_persisted_as_json_and_ids_are_monotonic(tmp_path) -> None:
    board = tmp_path / "board.json"
    repository = TaskRepository(board)

    first = repository.add_task("Inspect the example", Priority.HIGH)
    second = repository.add_task("Run the tests", "low")

    reopened = TaskRepository(board)
    assert [task.id for task in reopened.list_tasks()] == [1, 2]
    assert first.priority is Priority.HIGH
    assert second.priority is Priority.LOW
    payload = json.loads(board.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["completed_count"] == 0


def test_completing_a_task_is_idempotent_and_counts_once(tmp_path) -> None:
    repository = TaskRepository(tmp_path / "board.json")
    task = repository.add_task("Fix the completion counter", "high")

    completed, first_changed = repository.complete_task(task.id)
    repeated, second_changed = repository.complete_task(task.id)

    assert completed.completed is True
    assert repeated.completed is True
    assert first_changed is True
    assert second_changed is False
    assert repository.completed_count() == 1


def test_invalid_domain_input_is_rejected_without_writing(tmp_path) -> None:
    board = tmp_path / "board.json"
    repository = TaskRepository(board)

    with pytest.raises(ValidationError, match="title"):
        repository.add_task("   ", "low")
    with pytest.raises(ValidationError, match="priority"):
        repository.add_task("A valid title", "urgent")

    assert not board.exists()


def test_missing_task_and_corrupt_json_fail_loudly(tmp_path) -> None:
    board = tmp_path / "board.json"
    repository = TaskRepository(board)
    with pytest.raises(TaskNotFoundError, match="99"):
        repository.complete_task(99)

    board.write_text('{"version": 1, "tasks": "not-an-array"}', encoding="utf-8")
    with pytest.raises(RepositoryError, match="completed_count"):
        repository.list_tasks()
