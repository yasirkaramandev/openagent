"""Application service for Task Board commands."""

from __future__ import annotations

from dataclasses import dataclass

from .models import Priority, Task
from .repository import TaskRepository


@dataclass(frozen=True, slots=True)
class CompletionResult:
    task: Task
    changed: bool


class TaskBoardService:
    def __init__(self, repository: TaskRepository) -> None:
        self.repository = repository

    def add(self, title: str, priority: Priority | str = Priority.MEDIUM) -> Task:
        return self.repository.add_task(title, priority)

    def list(self) -> list[Task]:
        return sorted(self.repository.list_tasks(), key=lambda task: task.id)

    def complete(self, task_id: int | str) -> CompletionResult:
        task, changed = self.repository.complete_task(task_id)
        return CompletionResult(task=task, changed=changed)

    def summary(self) -> dict[str, int]:
        tasks = self.list()
        completed = self.repository.completed_count()
        return {
            "total": len(tasks),
            "completed": completed,
            "pending": len(tasks) - completed,
        }
