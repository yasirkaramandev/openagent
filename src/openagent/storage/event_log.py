"""SQLite-authoritative event store with a batched, streaming JSONL export (spec §5, §6).

SQLite is the source of truth. ``events.jsonl`` is a **projection** of it: a plain-text file that
tools, editors and humans can read without a database. Keeping that distinction straight is what
makes the export cheap to get right.

The old implementation did not. Every ``append`` wrote one row to SQLite and then rebuilt the whole
JSONL file from every event in the run — so n appends read 1 + 2 + … + n rows and rewrote the file
n times, and each rewrite built the entire file as one Python string first. A streaming model emits
thousands of ``message.delta`` events per turn, so this was not a theoretical cost: 10 000 events
meant ~50 million row reads, 10 000 full-file rewrites, and a peak memory footprint the size of the
finished file.

The export is now:

* **batched** — a dirty counter plus a short deadline, so a burst of deltas costs one rewrite rather
  than one per event;
* **mandatory at the end** — a terminal event always flushes, because that is the file a user opens
  once the run finishes. Batching may delay the projection; it must never truncate it;
* **streamed** — events are pulled from SQLite in pages and written line by line to a temp file that
  atomically replaces the old one, so peak memory does not track file size;
* **locked by the OS** — see ``security.file_lock``. The old lock deleted any lock file older than
  30 seconds, which meant a slow-but-alive export had its lock stolen exactly when it mattered.

A crash between the SQLite commit and the export loses nothing: the events are committed, and the
projection is stale. ``repair()`` (and ``openagent events repair``) rebuilds it from SQLite, and is
idempotent — running it twice produces the same bytes.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from ..core.events import NormalizedEvent, is_terminal_event_type
from ..credentials.redaction import redact_mapping
from ..security.atomic import atomic_write_lines
from ..security.file_lock import file_lock
from .repositories import EventIndexRepository

_IS_WINDOWS = sys.platform.startswith("win")

#: Flush after this many un-exported events. Sized so a streaming turn does a handful of rewrites
#: instead of thousands, while a quiet run still exports promptly via the deadline below.
DEFAULT_BATCH_SIZE = 64
#: …or after this long, whichever comes first, so a slow trickle of events is not left unexported
#: for an unbounded time. A live TUI reads the DB, not this file, so the delay is not user-visible.
DEFAULT_MAX_DELAY_SECONDS = 0.25
#: How long an export waits for a competing exporter before giving up and reporting a timeout.
EXPORT_LOCK_TIMEOUT_SECONDS = 30.0
#: Rows fetched per page while streaming.
EXPORT_PAGE_SIZE = 500


def _secure_dir(path: Path) -> None:
    if not _IS_WINDOWS:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)


class EventLog:
    """Persists event JSON in SQLite and keeps a JSONL projection of it on disk."""

    def __init__(
        self,
        run_dir: Path,
        index: EventIndexRepository | None = None,
        *,
        run_id: str | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_delay: float = DEFAULT_MAX_DELAY_SECONDS,
    ) -> None:
        self.run_dir = run_dir
        self.path = run_dir / "events.jsonl"
        self.index = index
        self._run_id = run_id
        self._batch_size = max(1, batch_size)
        self._max_delay = max(0.0, max_delay)
        #: Events committed to SQLite but not yet reflected in the JSONL projection.
        self._pending = 0
        self._last_export = time.monotonic()
        #: The highest sequence this instance has written to the file, and the file size it left
        #: behind. Together they are the evidence that appending is safe; see ``_resume_point``.
        self._exported_seq = 0
        self._exported_size = -1
        self._exported_inode = -1

    # ------------------------------------------------------------------ writing

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
            atomic_write_lines(self.path, (item.to_json_line() for item in events), mode=0o600)
            return safe

        self.index.append_event(safe)
        self._pending += 1
        if self._should_export(safe):
            self.export()
        return safe

    def _should_export(self, event: NormalizedEvent) -> bool:
        # A terminal event is the one moment the projection must be complete: the run is over and
        # this file is what the user (and the artifact bundle) reads.
        if is_terminal_event_type(event.type):
            return True
        # The first event always materialises the file. A run that has started should be visible on
        # disk immediately — someone tailing events.jsonl, or a recovery path reading it, should not
        # have to wait for a batch to fill. It costs one export per run.
        if self._exported_seq == 0:
            return True
        if self._pending >= self._batch_size:
            return True
        return (time.monotonic() - self._last_export) >= self._max_delay

    def export(self, *, full: bool = False) -> Path:
        """Bring the JSONL projection up to date with SQLite. Safe to call at any time.

        Normally this **appends** only the events the file does not have yet, which is what makes the
        whole-run cost linear: the projection is append-only by definition, so rewriting the earlier
        lines would reproduce exactly the quadratic behaviour being removed. A full rewrite happens
        when appending cannot be proven safe — see :meth:`_resume_point`.
        """

        if self.index is None:
            return self.path
        run_id = self._run_id or self.run_dir.name
        self.run_dir.mkdir(parents=True, exist_ok=True)
        _secure_dir(self.run_dir)
        # Take the lock *before* reading, so an older reader can never replace a newer snapshot.
        with file_lock(self._lock_path(), timeout=EXPORT_LOCK_TIMEOUT_SECONDS):
            resume = 0 if full else self._resume_point()
            if resume > 0:
                self._append_from(run_id, resume)
            else:
                self._rewrite(run_id)
        self._pending = 0
        self._last_export = time.monotonic()
        return self.path

    def flush(self) -> Path:
        """Export only if something is pending. Cheap to call on shutdown paths."""

        if self.index is not None and self._pending:
            return self.export()
        return self.path

    def repair(self) -> Path:
        """Rebuild the projection from SQLite from scratch, whatever state the file is in.

        Always a full rewrite: repair exists for the cases where the file cannot be trusted (a crash
        mid-append, a truncated tail, an edit by hand), so resuming from it would defeat the point.
        Idempotent — running it twice produces identical bytes.
        """

        return self.export(full=True)

    def _lock_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.lock")

    def _resume_point(self) -> int:
        """The sequence to append after, or 0 to demand a full rewrite.

        Appending is only safe when this instance can account for every byte in the file: we know
        which sequence we last wrote, and the file is still the exact one we left behind. Any other
        state — a fresh process, another writer, a truncated tail, a hand edit — falls back to a
        full rewrite rather than appending onto something unknown.

        Both size *and* inode are checked, because the two ways the file can change leave different
        traces. Another writer appending changes the size; another writer doing a full rewrite goes
        through ``os.replace``, which installs a **new inode** at the same path and can land on the
        same size. Checking only one of them would miss the other.
        """

        if self._exported_seq <= 0:
            return 0
        try:
            info = self.path.stat()
        except OSError:
            return 0
        if info.st_size != self._exported_size or info.st_ino != self._exported_inode:
            return 0
        return self._exported_seq

    def _append_from(self, run_id: str, after_seq: int) -> None:
        assert self.index is not None
        rows = self.index.iter_event_rows(run_id, after_seq=after_seq, batch_size=EXPORT_PAGE_SIZE)
        buffer: list[str] = []
        last_seq = after_seq
        written = False
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
        try:
            for seq, event in rows:
                buffer.append(event.to_json_line())
                last_seq = seq
                if len(buffer) >= EXPORT_PAGE_SIZE:
                    os.write(fd, ("\n".join(buffer) + "\n").encode("utf-8"))
                    buffer.clear()
                    written = True
            if buffer:
                os.write(fd, ("\n".join(buffer) + "\n").encode("utf-8"))
                written = True
            if written:
                os.fsync(fd)
        finally:
            os.close(fd)
        self._exported_seq = last_seq
        self._remember_file()

    def _rewrite(self, run_id: str) -> None:
        assert self.index is not None
        state: dict[str, int] = {"last_seq": 0}

        def _lines() -> Iterator[str]:
            assert self.index is not None
            for seq, event in self.index.iter_event_rows(run_id, batch_size=EXPORT_PAGE_SIZE):
                state["last_seq"] = seq
                yield event.to_json_line()

        atomic_write_lines(self.path, _lines(), mode=0o600)
        self._exported_seq = state["last_seq"]
        self._remember_file()

    def _remember_file(self) -> None:
        """Record the identity of the file we just left behind, for ``_resume_point``."""

        try:
            info = self.path.stat()
        except OSError:
            self._exported_size = -1
            self._exported_inode = -1
            return
        self._exported_size = info.st_size
        self._exported_inode = info.st_ino

    # ------------------------------------------------------------------ reading

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
            yield from self.index.iter_events(run_id, batch_size=EXPORT_PAGE_SIZE)
            return
        yield from self._read_file()

    def read_raw(self) -> list[dict]:
        return [event.model_dump(mode="json") for event in self.read()]

    # ------------------------------------------------------------------ consistency

    def pending_export_count(self) -> int:
        """How far the JSONL projection is behind SQLite, for Doctor's consistency check."""

        return self._pending

    def exported_line_count(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
