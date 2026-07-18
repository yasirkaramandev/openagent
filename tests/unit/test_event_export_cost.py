"""The event export must not cost O(n²) (spec §5).

Before v0.1.4, ``EventLog.append`` wrote the event to SQLite and then rebuilt the *entire*
``events.jsonl`` from scratch — every time. Appending n events therefore read 1 + 2 + … + n rows and
rewrote the whole file n times. A streaming model emits thousands of ``message.delta`` events in a
single turn, so the quadratic term is not theoretical: at 10 000 events the old code did ~50 million
row reads and 10 000 full-file rewrites to produce one file.

Measuring wall-clock here would be flaky and would not say *why* it is slow, so these tests assert
the algorithmic shape directly: how many full exports happen, how many rows they read, and whether
the writer ever materialises the whole file as one Python string.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openagent.core.events import EventType, NormalizedEvent
from openagent.storage.db import Database
from openagent.storage.event_log import EventLog
from openagent.storage.repositories import EventIndexRepository

RUN_ID = "run_cost"


@pytest.fixture()
def index(tmp_path: Path) -> EventIndexRepository:
    return EventIndexRepository(Database.open(tmp_path / "openagent.db"))


def _event(n: int, *, type_: EventType = EventType.MESSAGE_DELTA) -> NormalizedEvent:
    return NormalizedEvent(
        id=f"evt_{n:06d}",
        run_id=RUN_ID,
        type=type_,
        source="api-agent",
        data={"text": f"chunk {n}"},
    )


class _CountingIndex(EventIndexRepository):
    """Counts full exports and the rows each one reads."""

    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self.full_reads = 0
        self.rows_read = 0
        self.stream_calls = 0

    def read(self, run_id: str):  # type: ignore[no-untyped-def]
        self.full_reads += 1
        rows = super().read(run_id)
        self.rows_read += len(rows)
        return rows

    def iter_event_rows(self, run_id, *, after_seq=0, batch_size=500):  # type: ignore[no-untyped-def]
        self.stream_calls += 1
        for row in super().iter_event_rows(run_id, after_seq=after_seq, batch_size=batch_size):
            self.rows_read += 1
            yield row


def test_appending_many_events_does_not_rewrite_the_file_each_time(tmp_path: Path) -> None:
    """The headline: n appends must not mean n full exports."""

    count = 10_000
    index = _CountingIndex(Database.open(tmp_path / "openagent.db"))
    log = EventLog(tmp_path / "run", index=index, run_id=RUN_ID)

    for n in range(count):
        log.append(_event(n))

    exports = index.full_reads + index.stream_calls
    assert exports < count / 10, (
        f"{exports} full exports for {count} appends — the export is still per-event, which is the "
        "quadratic behaviour this test exists to prevent"
    )
    # Quadratic would be ~n²/2 = 50_000_000 rows. A batched export reads each event a bounded
    # number of times.
    assert index.rows_read < count * 20, (
        f"{index.rows_read} rows read for {count} events; the export is re-reading the whole run "
        "far too often"
    )


def test_terminal_event_forces_a_flush(tmp_path: Path) -> None:
    """Batching is an optimisation, not a licence to lose the ending.

    Whatever the batching policy, once the run reaches a terminal event the exported file must be
    complete — that is the file a user reads after the run finishes.
    """

    index = EventIndexRepository(Database.open(tmp_path / "openagent.db"))
    run_dir = tmp_path / "run"
    log = EventLog(run_dir, index=index, run_id=RUN_ID)

    for n in range(1_000):
        log.append(_event(n))
    log.append(_event(1_000, type_=EventType.RUN_COMPLETED))

    exported = (run_dir / "events.jsonl").read_text().strip().splitlines()
    assert len(exported) == 1_001
    assert len(index.read(RUN_ID)) == 1_001


def test_sequences_are_complete_and_ordered(tmp_path: Path) -> None:
    """Batching must not perturb ordering or drop an allocation."""

    index = EventIndexRepository(Database.open(tmp_path / "openagent.db"))
    log = EventLog(tmp_path / "run", index=index, run_id=RUN_ID)
    for n in range(500):
        log.append(_event(n))
    log.append(_event(500, type_=EventType.RUN_COMPLETED))

    assert index.sequences_for(RUN_ID) == list(range(1, 502))
    exported = (tmp_path / "run" / "events.jsonl").read_text().strip().splitlines()
    ids = [line.split('"id":"')[1].split('"')[0] for line in exported]
    assert ids == [f"evt_{n:06d}" for n in range(501)]


def test_export_streams_instead_of_building_one_string(tmp_path: Path, monkeypatch) -> None:
    """Peak memory must not track file size.

    Asserted structurally rather than by sampling RSS: the export must not call the list-returning
    ``read``, and must not hand a fully-built body to ``atomic_write_text``. Both of those are what
    "build the whole file in RAM first" looks like in this codebase.
    """

    import openagent.storage.event_log as event_log_module

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("export built the entire file as one string in memory")

    monkeypatch.setattr(event_log_module, "atomic_write_text", _forbidden, raising=False)

    index = _CountingIndex(Database.open(tmp_path / "openagent.db"))
    log = EventLog(tmp_path / "run", index=index, run_id=RUN_ID)
    for n in range(200):
        log.append(_event(n))
    log.append(_event(200, type_=EventType.RUN_COMPLETED))

    assert index.full_reads == 0, "the export used the list-returning read() instead of streaming"
    assert (tmp_path / "run" / "events.jsonl").exists()


def test_crash_before_export_keeps_sqlite_authoritative(tmp_path: Path) -> None:
    """SQLite is the source of truth; a missed export is a stale projection, never data loss."""

    index = EventIndexRepository(Database.open(tmp_path / "openagent.db"))
    run_dir = tmp_path / "run"
    log = EventLog(run_dir, index=index, run_id=RUN_ID)
    for n in range(100):
        log.append(_event(n))

    # Simulate the process dying before the pending batch was flushed.
    exported_lines = 0
    path = run_dir / "events.jsonl"
    if path.exists():
        exported_lines = len(path.read_text().strip().splitlines())
    assert len(index.read(RUN_ID)) == 100, "SQLite must hold every event regardless of the export"
    assert exported_lines <= 100

    # A fresh log over the same run repairs the projection from SQLite.
    repaired = EventLog(run_dir, index=index, run_id=RUN_ID).repair()
    assert len(repaired.read_text().strip().splitlines()) == 100


def test_repair_is_idempotent(tmp_path: Path) -> None:
    index = EventIndexRepository(Database.open(tmp_path / "openagent.db"))
    run_dir = tmp_path / "run"
    log = EventLog(run_dir, index=index, run_id=RUN_ID)
    for n in range(50):
        log.append(_event(n))

    first = log.repair().read_bytes()
    second = log.repair().read_bytes()
    assert first == second
    assert len(first.decode().strip().splitlines()) == 50
