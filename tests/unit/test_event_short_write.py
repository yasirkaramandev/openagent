"""Incremental event export handles POSIX short writes without losing bytes or cursor state."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from openagent.core.events import EventType, NormalizedEvent
from openagent.storage.db import Database
from openagent.storage.event_log import EventExportError, EventLog
from openagent.storage.repositories import EventIndexRepository


def _event(number: int) -> NormalizedEvent:
    return NormalizedEvent(
        id=f"evt_{number}",
        run_id="run_short_write",
        type=EventType.MESSAGE_DELTA,
        source="test",
        data={"text": f"payload-{number}"},
    )


def test_incremental_export_retries_until_every_byte_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = EventIndexRepository(Database.open(tmp_path / "openagent.db"))
    log = EventLog(tmp_path / "run", index=index, run_id="run_short_write", batch_size=1)
    log.append(_event(1))  # initial atomic rewrite establishes a safe append point

    real_write = os.write
    calls = 0

    def short_write(fd: int, payload: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        return real_write(fd, bytes(payload[: min(len(payload), (calls % 7) + 1)]))

    monkeypatch.setattr("openagent.storage.event_log.os.write", short_write)
    log.append(_event(2))

    rows = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert [row["id"] for row in rows] == ["evt_1", "evt_2"]
    assert calls > 1, "the injected short write was not retried"


def test_zero_progress_write_raises_and_does_not_advance_export_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = EventIndexRepository(Database.open(tmp_path / "openagent.db"))
    log = EventLog(tmp_path / "run", index=index, run_id="run_short_write", batch_size=1)
    log.append(_event(1))
    before_seq = log._exported_seq

    monkeypatch.setattr("openagent.storage.event_log.os.write", lambda _fd, _payload: 0)
    with pytest.raises(EventExportError, match="SQLite event committed") as raised:
        log.append(_event(2))
    assert isinstance(raised.value.__cause__, OSError)
    assert "made no progress" in str(raised.value.__cause__)

    assert log._exported_seq == before_seq
    assert [event.id for event in index.read("run_short_write")] == ["evt_1", "evt_2"]
    assert [json.loads(line)["id"] for line in log.path.read_text().splitlines()] == ["evt_1"]
