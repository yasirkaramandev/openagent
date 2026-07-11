"""Append-only event log (spec §34).

``events.jsonl`` in each run directory is the source of truth for events — human-inspectable,
recoverable if the DB is lost, and easy to stream. The SQLite ``events`` table stores only an index
(id, seq, type, timestamp, source). Every event is redacted before it hits disk (spec §30).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from ..core.events import NormalizedEvent
from ..credentials.redaction import redact_mapping
from .repositories import EventIndexRepository


class EventLog:
    """Writes normalized events to a run's ``events.jsonl`` and indexes them in SQLite."""

    def __init__(self, run_dir: Path, index: EventIndexRepository | None = None) -> None:
        self.run_dir = run_dir
        self.path = run_dir / "events.jsonl"
        self.index = index

    def append(self, event: NormalizedEvent) -> NormalizedEvent:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        safe = event.model_copy(update={"data": redact_mapping(event.data)})
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(safe.to_json_line() + "\n")
        if self.index is not None:
            seq = self.index.next_seq(safe.run_id)
            type_ = safe.type if isinstance(safe.type, str) else safe.type.value
            self.index.add(safe.id, safe.run_id, seq, type_, safe.timestamp, safe.source)
        return safe

    def read(self) -> Iterator[NormalizedEvent]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield NormalizedEvent.model_validate(json.loads(line))

    def read_raw(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out
