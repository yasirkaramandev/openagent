"""Validated, atomic JSON persistence for the Task Board example."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .models import Priority, Task, ValidationError, parse_task_id

FORMAT_VERSION = 1


class RepositoryError(RuntimeError):
    """The board file is unreadable, unsafe, or structurally invalid."""


class TaskNotFoundError(LookupError):
    """No task has the requested id."""


class TaskRepository:
    """Persist a task board in one human-readable JSON file.

    Writes use a sibling temporary file plus ``os.replace`` so readers observe
    either the previous complete board or the new complete board.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def list_tasks(self) -> list[Task]:
        state = self._load()
        return [Task.from_dict(record) for record in state["tasks"]]

    def add_task(self, title: str, priority: Priority | str = Priority.MEDIUM) -> Task:
        state = self._load()
        tasks = [Task.from_dict(record) for record in state["tasks"]]
        next_id = max((task.id for task in tasks), default=0) + 1
        task = Task(id=next_id, title=title, priority=Priority.parse(priority))
        state["tasks"].append(task.as_dict())
        self._save(state)
        return task

    def complete_task(self, task_id: int | str) -> tuple[Task, bool]:
        """Complete a task once and return ``(task, changed)``.

        The idempotence guard is the fix taught by the controlled bug scenario:
        completing an already-completed task must not increment the counter.
        """

        wanted = parse_task_id(task_id)
        state = self._load()
        for index, record in enumerate(state["tasks"]):
            current = Task.from_dict(record)
            if current.id != wanted:
                continue
            # SCENARIO_COMPLETION_GUARD_START
            if current.completed:
                return current, False
            # SCENARIO_COMPLETION_GUARD_END
            completed = Task(
                id=current.id,
                title=current.title,
                priority=current.priority,
                completed=True,
            )
            state["tasks"][index] = completed.as_dict()
            state["completed_count"] += 1
            self._save(state)
            return completed, True
        raise TaskNotFoundError(f"task {wanted} was not found")

    def completed_count(self) -> int:
        return int(self._load()["completed_count"])

    def _load(self) -> dict[str, Any]:
        if self.path.is_symlink():
            raise RepositoryError("refusing to read a board through a symbolic link")
        if not self.path.exists():
            return {"version": FORMAT_VERSION, "completed_count": 0, "tasks": []}
        if not self.path.is_file():
            raise RepositoryError("board path is not a regular file")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RepositoryError("board file is not valid UTF-8 JSON") from exc
        return self._validate_state(raw)

    @staticmethod
    def _validate_state(raw: object) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise RepositoryError("board JSON must be an object")
        if raw.get("version") != FORMAT_VERSION:
            raise RepositoryError(f"board version must be {FORMAT_VERSION}")
        counter = raw.get("completed_count")
        if isinstance(counter, bool) or not isinstance(counter, int) or counter < 0:
            raise RepositoryError("completed_count must be a non-negative integer")
        records = raw.get("tasks")
        if not isinstance(records, list):
            raise RepositoryError("tasks must be an array")
        tasks: list[Task] = []
        try:
            for record in records:
                if not isinstance(record, dict):
                    raise ValidationError("each task must be an object")
                tasks.append(Task.from_dict(record))
        except ValidationError as exc:
            raise RepositoryError(str(exc)) from exc
        ids = [task.id for task in tasks]
        if len(ids) != len(set(ids)):
            raise RepositoryError("task ids must be unique")
        return {
            "version": FORMAT_VERSION,
            "completed_count": counter,
            "tasks": [task.as_dict() for task in tasks],
        }

    def _save(self, state: dict[str, Any]) -> None:
        validated = self._validate_state(state)
        if self.path.is_symlink():
            raise RepositoryError("refusing to replace a board symbolic link")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(validated, stream, indent=2, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        except OSError as exc:
            raise RepositoryError("could not persist the board atomically") from exc
