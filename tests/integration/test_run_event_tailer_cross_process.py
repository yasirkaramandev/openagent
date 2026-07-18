from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from openagent.services.run_event_tailer import RunEventTailer
from openagent.storage.db import Database
from openagent.storage.repositories import EventIndexRepository


async def test_console_tailer_observes_another_process_and_isolates_run(tmp_path: Path):
    database_path = tmp_path / "openagent.db"
    repository = EventIndexRepository(Database.open(database_path))
    delivered = []
    arrived = asyncio.Event()

    def on_events(events):
        delivered.extend(events)
        if any(event.data.get("text") == "from process B" for event in events):
            arrived.set()

    tailer = RunEventTailer(
        "run_visible",
        repository,
        on_events,
        poll_interval_active=0.05,
        poll_interval_idle=0.05,
        poll_interval_long_idle=0.1,
        force_probe_interval=0.1,
    )
    await tailer.initial_replay()
    task = asyncio.create_task(tailer.run())

    writer = """
import json
import sys
from pathlib import Path
from openagent.core.events import EventType, NormalizedEvent
from openagent.storage.db import Database
from openagent.storage.repositories import EventIndexRepository

repo = EventIndexRepository(Database.open(Path(sys.argv[1])))
repo.append_event(NormalizedEvent(
    id="evt_other_process",
    run_id="run_other",
    type=EventType.MESSAGE_COMPLETED,
    source="process-b",
    data={"item_id": "m", "text": "other project run"},
))
repo.append_event(NormalizedEvent(
    id="evt_visible_process",
    run_id="run_visible",
    type=EventType.MESSAGE_COMPLETED,
    source="process-b",
    data={"item_id": "m", "text": "from process B"},
))
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        writer,
        str(database_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    assert process.returncode == 0, (stdout + stderr).decode(errors="replace")
    await asyncio.wait_for(arrived.wait(), timeout=3)

    await tailer.stop()
    await asyncio.wait_for(task, timeout=1)

    assert [event.id for event in delivered] == ["evt_visible_process"]
    assert "run_other" not in json.dumps([event.model_dump(mode="json") for event in delivered])
