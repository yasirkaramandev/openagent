"""Domain values for the Task Board example."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ValidationError(ValueError):
    """Raised when untrusted task input does not satisfy the domain rules."""


class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @classmethod
    def parse(cls, value: object) -> Priority:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise ValidationError("priority must be one of: low, medium, high")
        try:
            return cls(value.strip().lower())
        except ValueError as exc:
            raise ValidationError("priority must be one of: low, medium, high") from exc


def _validate_title(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("title must be a string")
    title = value.strip()
    if not title:
        raise ValidationError("title must not be empty")
    if len(title) > 120:
        raise ValidationError("title must be at most 120 characters")
    if any(ord(character) < 32 for character in title):
        raise ValidationError("title must not contain control characters")
    return title


def _validate_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError("task id must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class Task:
    """One persistent task."""

    id: int
    title: str
    priority: Priority = Priority.MEDIUM
    completed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _validate_id(self.id))
        object.__setattr__(self, "title", _validate_title(self.title))
        object.__setattr__(self, "priority", Priority.parse(self.priority))
        if not isinstance(self.completed, bool):
            raise ValidationError("completed must be a boolean")

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "priority": self.priority.value,
            "completed": self.completed,
        }

    @classmethod
    def from_dict(cls, record: Mapping[str, Any]) -> Task:
        required = {"id", "title", "priority", "completed"}
        missing = required.difference(record)
        if missing:
            raise ValidationError(f"task record is missing: {', '.join(sorted(missing))}")
        return cls(
            id=record["id"],
            title=record["title"],
            priority=record["priority"],
            completed=record["completed"],
        )


def parse_task_id(value: object) -> int:
    """Parse a CLI/service task id without accepting booleans or fractional values."""

    if isinstance(value, int):
        return _validate_id(value)
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise ValidationError("task id must be a positive integer")
    return _validate_id(int(value))
