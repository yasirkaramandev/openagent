"""SQLite-authoritative event store with atomic JSONL export."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..core.events import NormalizedEvent
from ..credentials.redaction import redact_mapping
from ..security.atomic import atomic_write_text
from .repositories import EventIndexRepository

_IS_WINDOWS = sys.platform.startswith("win")


def _secure_dir(path: Path) -> None:
    if not _IS_WINDOWS:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)


@contextmanager
def _export_lock(path: Path, *, timeout: float = 10.0) -> Iterator[None]:
    lock = path.with_name(f".{path.name}.lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, f"{os.getpid()}\n".encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                stale = time.time() - lock.stat().st_mtime > 30.0
            except OSError:
                stale = False
            if stale:
                with contextlib.suppress(OSError):
                    lock.unlink()
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for event export lock {lock}") from None
            time.sleep(0.01)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


class EventLog:
    """Persists event JSON in SQLite, then atomically exports the complete JSONL projection."""

    def __init__(
        self,
        run_dir: Path,
        index: EventIndexRepository | None = None,
        *,
        run_id: str | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.path = run_dir / "events.jsonl"
        self.index = index
        self._run_id = run_id

    def append(self, event: NormalizedEvent) -> NormalizedEvent:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        _secure_dir(self.run_dir)
        safe = event.model_copy(update={"data": redact_mapping(event.data)})
        self._run_id = safe.run_id
        if self.index is None:
            # Standalone tests/tools have no DB. Keep the same atomic-export semantics by treating
            # the existing JSONL as the local body store.
            events = list(self._read_file())
            if any(existing.id == safe.id for existing in events):
                raise ValueError(f"duplicate event id {safe.id}")
            events.append(safe)
            self._write_export(events)
            return safe
        self.index.append_event(safe)
        self.export()
        return safe

    def export(self) -> Path:
        if self.index is None:
            return self.path
        run_id = self._run_id or self.run_dir.name
        with _export_lock(self.path):
            # Read only after acquiring the cross-process export lock. An older writer can therefore
            # never overwrite a newer snapshot after both committed to SQLite.
            self._write_export(self.index.read(run_id))
        return self.path

    def _write_export(self, events: list[NormalizedEvent]) -> None:
        body = "".join(event.to_json_line() + "\n" for event in events)
        atomic_write_text(self.path, body, mode=0o600)

    def _read_file(self) -> Iterator[NormalizedEvent]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield NormalizedEvent.model_validate(json.loads(line))

    def read(self) -> Iterator[NormalizedEvent]:
        if self.index is not None:
            run_id = self._run_id or self.run_dir.name
            yield from self.index.read(run_id)
            return
        yield from self._read_file()

    def read_raw(self) -> list[dict]:
        return [event.model_dump(mode="json") for event in self.read()]
