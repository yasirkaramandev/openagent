"""Standard-library command line interface for the Task Board."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .models import Priority, ValidationError
from .repository import RepositoryError, TaskNotFoundError, TaskRepository
from .service import TaskBoardService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="task-board", description="Offline JSON task board")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("task-board.json"),
        help="board JSON path (default: ./task-board.json)",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    add = subcommands.add_parser("add", help="add a task")
    add.add_argument("title")
    add.add_argument(
        "--priority", choices=[priority.value for priority in Priority], default="medium"
    )
    add.add_argument("--json", action="store_true", dest="json_out")

    listing = subcommands.add_parser("list", help="list tasks")
    listing.add_argument("--json", action="store_true", dest="json_out")

    complete = subcommands.add_parser("complete", help="complete one task")
    complete.add_argument("task_id")
    complete.add_argument("--json", action="store_true", dest="json_out")

    summary = subcommands.add_parser("summary", help="show task counts")
    summary.add_argument("--json", action="store_true", dest="json_out")
    return parser


def _emit_json(value: object) -> None:
    print(json.dumps(value, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = TaskBoardService(TaskRepository(args.db))
    try:
        if args.command == "add":
            task = service.add(args.title, args.priority)
            if args.json_out:
                _emit_json({"task": task.as_dict()})
            else:
                print(f"added #{task.id}: {task.title} [{task.priority.value}]")
        elif args.command == "list":
            tasks = service.list()
            if args.json_out:
                _emit_json({"tasks": [task.as_dict() for task in tasks]})
            elif not tasks:
                print("No tasks.")
            else:
                for task in tasks:
                    mark = "x" if task.completed else " "
                    print(f"[{mark}] #{task.id} {task.title} [{task.priority.value}]")
        elif args.command == "complete":
            result = service.complete(args.task_id)
            payload = {"changed": result.changed, "task": result.task.as_dict()}
            if args.json_out:
                _emit_json(payload)
            else:
                action = "completed" if result.changed else "already completed"
                print(f"#{result.task.id}: {action}")
        elif args.command == "summary":
            summary = service.summary()
            if args.json_out:
                _emit_json(summary)
            else:
                print(
                    f"{summary['total']} total, {summary['completed']} completed, "
                    f"{summary['pending']} pending"
                )
    except (RepositoryError, TaskNotFoundError, ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
