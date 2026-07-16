from __future__ import annotations

import sys
from pathlib import Path

import pytest

from openagent.core.events import EventType
from openagent.runtimes.cli.base import run_managed_cli
from openagent.security.process import ManagedProcess, minimal_environment


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script", "line_limit", "total_limit"),
    [
        ("print('x' * 65)", 64, 1024),
        ("print('1234567890')\nprint('abcdefghij')", 64, 16),
    ],
)
async def test_managed_cli_fails_visibly_when_stdout_budget_is_exceeded(
    tmp_path: Path, script: str, line_limit: int, total_limit: int
) -> None:
    proc = ManagedProcess(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=minimal_environment(),
        max_stdout_line_bytes=line_limit,
        max_stdout_total_bytes=total_limit,
    )

    events = [
        event
        async for event in run_managed_cli(
            proc=proc,
            run_id="run-limit",
            source="test-cli",
            mapper=lambda _obj, _run_id: [],
        )
    ]

    terminal = events[-1]
    assert terminal.type == EventType.RUN_FAILED.value
    assert terminal.data["error_type"] == "output_limit_exceeded"
    assert terminal.data["truncated"] is True
    assert proc.stdout_limit_exceeded


@pytest.mark.asyncio
async def test_stdout_at_limits_remains_readable(tmp_path: Path) -> None:
    proc = ManagedProcess(
        [sys.executable, "-c", "import sys; sys.stdout.write('1234\\n5678')"],
        cwd=tmp_path,
        env=minimal_environment(),
        max_stdout_line_bytes=4,
        max_stdout_total_bytes=9,
    )
    await proc.start()
    lines = [line async for line in proc.stream_stdout()]
    assert await proc.wait() == 0
    assert lines == ["1234", "5678"]
    assert not proc.stdout_limit_exceeded
