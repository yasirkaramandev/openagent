from __future__ import annotations

import asyncio
import time
from pathlib import Path

from sqlalchemy import insert

from openagent.core.events import EventType, NormalizedEvent
from openagent.core.projection import RunProjection
from openagent.services.run_event_tailer import BoundedEventIds, RunEventTailer
from openagent.storage import db as tables
from openagent.storage.db import Database
from openagent.storage.repositories import EventIndexRepository


def _event(run_id: str, number: int, event_type: EventType = EventType.MESSAGE_DELTA):
    return NormalizedEvent(
        id=f"evt_{number:016d}",
        run_id=run_id,
        type=event_type,
        source="test",
        data={"item_id": "message", "delta": str(number), "text": str(number)},
    )


def _repository(tmp_path: Path) -> EventIndexRepository:
    return EventIndexRepository(Database.open(tmp_path / "tailer.db"))


def test_bounded_event_ids_is_an_lru_set():
    seen = BoundedEventIds(2)
    assert seen.add("a") is True
    assert seen.add("b") is True
    assert seen.add("a") is False  # refreshes a, making b the oldest
    assert seen.add("c") is True
    assert "a" in seen and "c" in seen and "b" not in seen


def test_initial_replay_pages_without_rereading_history(tmp_path: Path, monkeypatch):
    repository = _repository(tmp_path)
    run_id = "run_history"
    for number in range(1, 1_201):
        repository.append_event(_event(run_id, number))
    calls: list[int] = []
    original = repository.iter_events_after

    def counted(run_id, after_seq, limit=500):
        calls.append(after_seq)
        yield from original(run_id, after_seq, limit)

    monkeypatch.setattr(repository, "iter_events_after", counted)
    delivered: list[NormalizedEvent] = []
    tailer = RunEventTailer(run_id, repository, delivered.extend, batch_size=137)

    replayed = asyncio.run(tailer.initial_replay())
    asyncio.run(tailer.stop())

    assert len(replayed) == 1_200
    assert len(delivered) == 1_200
    assert calls[0] == 0
    assert calls == sorted(calls)
    assert len(calls) == 9
    assert tailer.last_seq == 1_200


def test_ten_thousand_event_history_stays_keyset_paginated(tmp_path: Path, monkeypatch):
    repository = _repository(tmp_path)
    run_id = "run_10k"
    events = [_event(run_id, number) for number in range(1, 10_001)]
    with repository.db.engine.begin() as conn:
        conn.execute(
            insert(tables.events),
            [
                {
                    "id": event.id,
                    "run_id": run_id,
                    "seq": number,
                    "type": event.type,
                    "timestamp": event.timestamp,
                    "source": event.source,
                    "body": event.model_dump(mode="json"),
                }
                for number, event in enumerate(events, start=1)
            ],
        )
        conn.execute(insert(tables.event_sequences).values(run_id=run_id, next_seq=10_001))

    after_sequences: list[int] = []
    original = repository.iter_events_after

    def counted(run_id, after_seq, limit=500):
        after_sequences.append(after_seq)
        yield from original(run_id, after_seq, limit)

    monkeypatch.setattr(repository, "iter_events_after", counted)
    delivered: list[NormalizedEvent] = []
    tailer = RunEventTailer(run_id, repository, delivered.extend, batch_size=500)

    started = time.perf_counter()
    asyncio.run(tailer.initial_replay())
    replay_seconds = time.perf_counter() - started
    repository.append_event(_event(run_id, 10_001))
    asyncio.run(tailer.poll_once(force=True))
    asyncio.run(tailer.stop())

    assert len(delivered) == 10_001
    assert tailer.last_seq == 10_001
    assert after_sequences[:3] == [0, 500, 1_000]
    assert after_sequences[-2:] == [10_000, 10_000]
    assert len(after_sequences) == 22
    assert after_sequences == sorted(after_sequences)
    assert replay_seconds < 10


def test_cross_connection_data_version_wakes_incremental_poll(tmp_path: Path):
    database_path = tmp_path / "tailer.db"
    reader = EventIndexRepository(Database.open(database_path))
    writer = EventIndexRepository(Database.open(database_path))
    delivered: list[NormalizedEvent] = []
    tailer = RunEventTailer("run_cross", reader, delivered.extend, force_probe_interval=60)

    async def scenario():
        await tailer.initial_replay()
        writer.append_event(_event("run_cross", 1))
        polled = await tailer.poll_once()
        await tailer.stop()
        return polled

    polled = asyncio.run(scenario())

    assert [event.id for event in polled] == ["evt_0000000000000001"]
    assert delivered == polled
    assert tailer.last_seq == 1


def test_local_event_then_database_copy_is_applied_once_but_advances_seq(tmp_path: Path):
    repository = _repository(tmp_path)
    event = _event("run_dupe", 1)
    delivered: list[NormalizedEvent] = []
    tailer = RunEventTailer("run_dupe", repository, delivered.extend)

    async def scenario():
        await tailer.initial_replay()
        assert tailer.mark_local(event) is True
        delivered.append(event)  # low-latency local UI application
        repository.append_event(event)
        await tailer.poll_once(force=True)
        await tailer.stop()

    asyncio.run(scenario())

    assert [item.id for item in delivered] == [event.id]
    assert tailer.last_seq == 1


def test_message_delta_duplicate_does_not_double_projection_text(tmp_path: Path):
    repository = _repository(tmp_path)
    run_id = "run_delta"
    started = NormalizedEvent(
        id="evt_started",
        run_id=run_id,
        type=EventType.MESSAGE_STARTED,
        source="test",
        data={"item_id": "m"},
    )
    delta = NormalizedEvent(
        id="evt_delta",
        run_id=run_id,
        type=EventType.MESSAGE_DELTA,
        source="test",
        data={"item_id": "m", "delta": "hello"},
    )
    projection = RunProjection(run_id)
    tailer = RunEventTailer(run_id, repository, lambda events: projection.apply_all(events))

    async def scenario():
        await tailer.initial_replay()
        for event in (started, delta):
            assert tailer.mark_local(event)
            projection.apply(event)
            repository.append_event(event)
        await tailer.poll_once(force=True)
        await tailer.stop()

    asyncio.run(scenario())

    assert projection.messages[0].text == "hello"
    assert tailer.last_seq == 2


def test_sequence_gap_warns_and_continues(tmp_path: Path):
    repository = _repository(tmp_path)
    run_id = "run_gap"
    first = _event(run_id, 1)
    third = _event(run_id, 3)
    repository.append_event(first)
    with repository.db.engine.begin() as conn:
        conn.execute(
            insert(tables.events).values(
                id=third.id,
                run_id=run_id,
                seq=3,
                type=third.type,
                timestamp=third.timestamp,
                source=third.source,
                body=third.model_dump(mode="json"),
            )
        )
    warnings: list[str] = []
    delivered: list[NormalizedEvent] = []
    tailer = RunEventTailer(run_id, repository, delivered.extend, on_warning=warnings.append)

    asyncio.run(tailer.initial_replay())
    asyncio.run(tailer.stop())

    assert [event.id for event in delivered] == [first.id, third.id]
    assert any("expected 2, found 3" in warning for warning in warnings)
    assert tailer.last_seq == 3


def test_malformed_body_is_reported_skipped_and_later_event_is_delivered(tmp_path: Path):
    repository = _repository(tmp_path)
    run_id = "run_malformed"
    first = _event(run_id, 1)
    third = _event(run_id, 3)
    repository.append_event(first)
    with repository.db.engine.begin() as conn:
        conn.execute(
            insert(tables.events).values(
                id="evt_bad",
                run_id=run_id,
                seq=2,
                type="message.delta",
                timestamp=first.timestamp,
                source="test",
                body={"not": "an event"},
            )
        )
        conn.execute(
            insert(tables.events).values(
                id=third.id,
                run_id=run_id,
                seq=3,
                type=third.type,
                timestamp=third.timestamp,
                source=third.source,
                body=third.model_dump(mode="json"),
            )
        )
    warnings: list[str] = []
    delivered: list[NormalizedEvent] = []
    tailer = RunEventTailer(run_id, repository, delivered.extend, on_warning=warnings.append)

    asyncio.run(tailer.initial_replay())
    asyncio.run(tailer.stop())

    assert [event.id for event in delivered] == [first.id, third.id]
    assert any("seq 2 is malformed" in warning for warning in warnings)
    assert tailer.last_seq == 3


def test_subscriber_exception_does_not_stop_cursor_progress(tmp_path: Path):
    repository = _repository(tmp_path)
    repository.append_event(_event("run_hook", 1))
    warnings: list[str] = []

    def broken(_events):
        raise RuntimeError("closed screen")

    tailer = RunEventTailer("run_hook", repository, broken, on_warning=warnings.append)
    asyncio.run(tailer.initial_replay())
    asyncio.run(tailer.stop())

    assert tailer.last_seq == 1
    assert any("closed screen" in warning for warning in warnings)


def test_terminal_event_gets_final_drain_and_stops(tmp_path: Path):
    repository = _repository(tmp_path)
    run_id = "run_terminal"
    repository.append_event(_event(run_id, 1, EventType.RUN_COMPLETED))
    delivered: list[NormalizedEvent] = []
    tailer = RunEventTailer(
        run_id,
        repository,
        delivered.extend,
        poll_interval_active=0.001,
        poll_interval_idle=0.001,
        poll_interval_long_idle=0.001,
        force_probe_interval=0.001,
    )

    async def scenario():
        await tailer.initial_replay()
        await asyncio.wait_for(tailer.run(), timeout=1)

    asyncio.run(scenario())

    assert [event.type for event in delivered] == [EventType.RUN_COMPLETED.value]
