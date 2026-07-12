"""CLI adapters must resolve every run to exactly one terminal event (spec §6.2, §43).

Drives a real subprocess (the fake CLI) through the production mapping + finalization helpers, so a
nonzero exit, an empty/malformed stream, or a killed process can never be reported as "completed".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openagent.core.events import EventType
from openagent.runtimes.cli.base import CliRunRequest
from tests.fakecli import FakeCliAdapter, write_fake_script

TERMINALS = {EventType.RUN_COMPLETED.value, EventType.RUN_FAILED.value, EventType.RUN_CANCELLED.value}


async def _run(adapter: FakeCliAdapter, workspace: Path) -> list:
    events = []
    async for event in adapter.start_run(CliRunRequest(run_id="run_t", prompt="go", workspace=workspace)):
        events.append(event)
    return events


def _terminals(events) -> list[str]:
    return [e.type if isinstance(e.type, str) else e.type.value for e in events
            if (e.type if isinstance(e.type, str) else e.type.value) in TERMINALS]


@pytest.fixture()
def script(tmp_path: Path) -> Path:
    return write_fake_script(tmp_path)


async def test_exit0_with_success_event_completes(script: Path, tmp_path: Path):
    events = await _run(FakeCliAdapter(script, mode="complete"), tmp_path)
    assert _terminals(events) == [EventType.RUN_COMPLETED.value]


async def test_exit0_no_event_is_failed(script: Path, tmp_path: Path):
    events = await _run(FakeCliAdapter(script, mode="silent0"), tmp_path)
    assert _terminals(events) == [EventType.RUN_FAILED.value]


async def test_exit1_no_event_is_failed(script: Path, tmp_path: Path):
    events = await _run(FakeCliAdapter(script, mode="fail1"), tmp_path)
    assert _terminals(events) == [EventType.RUN_FAILED.value]


async def test_exit1_malformed_json_is_failed(script: Path, tmp_path: Path):
    events = await _run(FakeCliAdapter(script, mode="malformed"), tmp_path)
    assert _terminals(events) == [EventType.RUN_FAILED.value]


async def test_usage_limit_is_failed(script: Path, tmp_path: Path):
    events = await _run(FakeCliAdapter(script, mode="usage_limit"), tmp_path)
    assert _terminals(events) == [EventType.RUN_FAILED.value]


async def test_exactly_one_terminal_event(script: Path, tmp_path: Path):
    for mode in ("complete", "silent0", "fail1", "malformed", "usage_limit"):
        events = await _run(FakeCliAdapter(script, mode=mode), tmp_path)
        assert len(_terminals(events)) == 1, f"{mode} produced {_terminals(events)}"
