"""Durable compensating-operation journal for DB/keychain/generated-file mutations."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .atomic import atomic_write_text
from .filesystem import SafeWorkspaceWalker, UnsafeWorkspacePath
from .process import PID_ALIVE, PID_UNKNOWN, pid_identity, run_process_status


@dataclass
class JournalOperation:
    journal: OperationJournal
    id: str
    kind: str
    stage: str
    payload: dict[str, Any]
    owner_pid: int | None = None
    owner_create_time: float | None = None

    def advance(self, stage: str) -> None:
        self.stage = stage
        self.journal._write(self)

    def complete(self) -> None:
        self.journal.complete(self.id)

    def is_owned_by_live_other_process(self) -> bool:
        """Refuse to recover an operation whose PID/start-time owner is still running."""

        if self.owner_pid is None or self.owner_pid == os.getpid():
            return False
        return run_process_status(self.owner_pid, self.owner_create_time) in {
            PID_ALIVE,
            PID_UNKNOWN,
        }


class OperationJournal:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def begin(self, kind: str, payload: dict[str, Any]) -> JournalOperation:
        owner_pid = os.getpid()
        operation = JournalOperation(
            self,
            f"op_{uuid.uuid4().hex}",
            kind,
            "begun",
            payload,
            owner_pid=owner_pid,
            owner_create_time=pid_identity(owner_pid),
        )
        self._write(operation)
        return operation

    def _write(self, operation: JournalOperation) -> None:
        # Payloads contain only references/ids. Callers must never pass secret values.
        atomic_write_text(
            self.root / f"{operation.id}.json",
            json.dumps(
                {
                    "id": operation.id,
                    "kind": operation.kind,
                    "stage": operation.stage,
                    "payload": operation.payload,
                    "owner_pid": operation.owner_pid,
                    "owner_create_time": operation.owner_create_time,
                },
                indent=2,
            ),
            mode=0o600,
        )

    def complete(self, operation_id: str) -> None:
        path = self.root / f"{operation_id}.json"
        path.unlink(missing_ok=True)

    def pending(self) -> list[JournalOperation]:
        operations: list[JournalOperation] = []
        try:
            walker = SafeWorkspaceWalker(self.root)
            files = walker.iter_files()
            for path in files:
                if path.suffix != ".json":
                    continue
                try:
                    payload = json.loads(walker.read_bytes(path.name).decode("utf-8"))
                    operations.append(
                        JournalOperation(
                            self,
                            id=str(payload["id"]),
                            kind=str(payload["kind"]),
                            stage=str(payload["stage"]),
                            payload=dict(payload.get("payload") or {}),
                            owner_pid=(
                                int(payload["owner_pid"])
                                if payload.get("owner_pid") is not None
                                else None
                            ),
                            owner_create_time=(
                                float(payload["owner_create_time"])
                                if payload.get("owner_create_time") is not None
                                else None
                            ),
                        )
                    )
                except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError):
                    continue
        except (OSError, UnsafeWorkspacePath):
            return []
        return operations
