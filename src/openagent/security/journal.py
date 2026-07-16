"""Durable compensating-operation journal for DB/keychain/generated-file mutations."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .atomic import atomic_write_text
from .filesystem import SafeWorkspaceWalker, UnsafeWorkspacePath


@dataclass
class JournalOperation:
    journal: OperationJournal
    id: str
    kind: str
    stage: str
    payload: dict[str, Any]

    def advance(self, stage: str) -> None:
        self.stage = stage
        self.journal._write(self)

    def complete(self) -> None:
        self.journal.complete(self.id)


class OperationJournal:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def begin(self, kind: str, payload: dict[str, Any]) -> JournalOperation:
        operation = JournalOperation(self, f"op_{uuid.uuid4().hex}", kind, "begun", payload)
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
                        )
                    )
                except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError):
                    continue
        except (OSError, UnsafeWorkspacePath):
            return []
        return operations
