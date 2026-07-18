"""Sequence allocation must not race (spec §11).

``EventLog.append`` allocated the sequence number like this::

    seq = self.index.next_seq(safe.run_id)   # SELECT max(seq)+1 on its own read connection
    self.index.add(..., seq, ...)            # INSERT on a *separate* write transaction

Two appenders to the same run — the CLI and the TUI, or two threads of one run — both read ``max=N``,
both compute ``N+1``, and both write it. Read-then-write across two connections is not atomic; there
is nothing between them holding the value.

The consequences got worse, not better, once migration m004 added the UNIQUE index on
``(run_id, seq)``: the JSONL line is written *before* the index insert, so the loser of the race now
gets an IntegrityError **after** its event is already on disk — an orphan line, a crashed caller, and
an index that disagrees with the log it indexes.

The fix is the one §11 asks for: allocate the sequence inside the same write transaction that
consumes it, and let the unique index be the backstop rather than the discovery mechanism.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from openagent.core.events import EventType, NormalizedEvent
from openagent.storage.db import Database
from openagent.storage.event_log import EventLog
from openagent.storage.repositories import EventIndexRepository


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database.open(tmp_path / "test.db")


def _event(run_id: str, n: int) -> NormalizedEvent:
    return NormalizedEvent(
        id=f"{run_id}-evt-{n}",
        run_id=run_id,
        type=EventType.RUN_STARTED,
        timestamp="2026-07-16T00:00:00Z",
        source="test",
        data={"n": n},
    )


def test_sequences_are_unique_under_concurrent_append(db: Database, tmp_path: Path):
    """The headline: N threads appending to one run must produce N distinct sequence numbers."""

    index = EventIndexRepository(db)
    log = EventLog(tmp_path / "run", index=index)
    run_id = "run_race"
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def append(n: int) -> None:
        try:
            barrier.wait(timeout=5)  # maximise the overlap on the read-then-write window
            log.append(_event(run_id, n))
        except BaseException as exc:  # noqa: BLE001 - re-raised via the assert below
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"append raised under concurrency: {errors[:3]}"
    seqs = index.sequences_for(run_id)
    assert len(seqs) == 8, f"expected 8 indexed events, got {len(seqs)}"
    assert len(set(seqs)) == 8, f"duplicate sequence numbers allocated: {sorted(seqs)}"
    assert sorted(seqs) == list(range(1, 9)), f"sequences are not contiguous 1..8: {sorted(seqs)}"


def test_the_index_and_the_log_agree_after_concurrent_append(db: Database, tmp_path: Path):
    """SQLite is the source of truth; once flushed, the JSONL projection matches it exactly.

    The flush is the point. Since v0.1.4 the export is batched, so during a run the file is allowed
    to lag behind the database — what is never allowed is for them to *disagree* once the projection
    is brought up to date. Six concurrent appends must produce six index rows and six lines, with no
    duplicate and no orphan line left behind by a loser of the sequence race.
    """

    index = EventIndexRepository(db)
    run_dir = tmp_path / "run"
    log = EventLog(run_dir, index=index)
    run_id = "run_agree"
    barrier = threading.Barrier(6)

    def append(n: int) -> None:
        barrier.wait(timeout=5)
        log.append(_event(run_id, n))

    threads = [threading.Thread(target=append, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    log.flush()

    exported = EventLog(run_dir).read_raw()
    indexed = index.sequences_for(run_id)
    assert len(exported) == len(indexed) == 6, (
        f"log has {len(exported)} lines but the index has {len(indexed)} rows"
    )
    assert sorted(indexed) == [1, 2, 3, 4, 5, 6]
    assert len({event["id"] for event in exported}) == 6, "the export contains a duplicate line"


def test_sequential_append_still_numbers_from_one(db: Database, tmp_path: Path):
    index = EventIndexRepository(db)
    log = EventLog(tmp_path / "run", index=index)
    for n in range(3):
        log.append(_event("run_seq", n))
    assert sorted(index.sequences_for("run_seq")) == [1, 2, 3]


def test_runs_are_numbered_independently(db: Database, tmp_path: Path):
    """A sequence is per-run, so a busy run must not push another run's numbering along."""

    index = EventIndexRepository(db)
    log_a = EventLog(tmp_path / "a", index=index)
    log_b = EventLog(tmp_path / "b", index=index)
    log_a.append(_event("run_a", 0))
    log_a.append(_event("run_a", 1))
    log_b.append(_event("run_b", 0))
    assert sorted(index.sequences_for("run_a")) == [1, 2]
    assert sorted(index.sequences_for("run_b")) == [1]


def test_a_duplicate_event_id_is_rejected(db: Database, tmp_path: Path):
    """The id is the primary key; re-appending the identical event must not silently double-index."""

    index = EventIndexRepository(db)
    log = EventLog(tmp_path / "run", index=index)
    event = _event("run_dup", 0)
    log.append(event)
    with pytest.raises(Exception):  # noqa: B017 - any integrity failure is acceptable here
        log.append(event)
    assert index.sequences_for("run_dup") == [1]
