"""Antigravity adapter mapping + registry (item 4).

The SUCCESS path is validated against a **recorded, real** fixture captured live from agy v1.1.0
(``tests/fixtures/antigravity_print.jsonl``). Failure/cancel mapping is checked with inline
synthetic objects — deliberately *not* recorded fixtures, since no real failure output was captured.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from openagent.runtimes.cli.antigravity import AntigravityAdapter, map_antigravity_event
from openagent.runtimes.cli.base import CliRunRequest
from openagent.runtimes.cli.registry import (
    build_cli_adapter,
    cli_display_name,
    cli_status_label,
    known_cli_types,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "antigravity_print.jsonl"


def _real_result() -> dict:
    line = FIXTURE.read_text(encoding="utf-8").strip().splitlines()[0]
    return json.loads(line)


def _types(events) -> list[str]:
    return [e.type if isinstance(e.type, str) else e.type.value for e in events]


# --------------------------------------------------------------------------- verified SUCCESS path

def test_maps_real_success_fixture():
    events = map_antigravity_event(_real_result(), "run_1")
    types = _types(events)
    assert "session.created" in types
    assert "message.completed" in types
    assert "usage.updated" in types
    assert types[-1] == "run.completed"

    session = next(e for e in events if _types([e]) == ["session.created"])
    assert session.data["provider_session_id"] == "938442f7-bca6-422b-b9d8-f8aa598de783"
    message = next(e for e in events if _types([e]) == ["message.completed"])
    assert message.data["text"] == "OK42\n"
    usage = next(e for e in events if _types([e]) == ["usage.updated"])
    assert usage.data["input_tokens"] == 39941
    assert usage.data["output_tokens"] == 7
    assert usage.data["provider_cost"] is None  # subscription product, no cost reported


def test_exactly_one_terminal_event_for_success():
    terminals = [t for t in _types(map_antigravity_event(_real_result(), "r"))
                 if t in ("run.completed", "run.failed", "run.cancelled")]
    assert terminals == ["run.completed"]


# --------------------------------------------------------------------------- fail-closed statuses

def test_cancelled_status_maps_to_cancelled():
    events = map_antigravity_event({"conversation_id": "c", "status": "CANCELLED"}, "r")
    assert _types(events)[-1] == "run.cancelled"


def test_unknown_status_fails():
    events = map_antigravity_event({"conversation_id": "c", "status": "UNKNOWN"}, "r")
    assert _types(events)[-1] == "run.failed"


def test_aborted_status_fails():
    events = map_antigravity_event({"status": "ABORTED", "error": "fatal"}, "r")
    terminal = next(e for e in events if _types([e]) == ["run.failed"])
    assert terminal.data["message"] == "fatal"


def test_missing_status_fails():
    assert _types(map_antigravity_event({"conversation_id": "c"}, "r"))[-1] == "run.failed"


# --------------------------------------------------------------------------- adapter args + registry

def test_start_run_uses_print_json_flags(tmp_path: Path):
    adapter = AntigravityAdapter(executable="/usr/local/bin/agy")
    args = adapter._build_args(
        CliRunRequest(run_id="r", prompt="do it", workspace=tmp_path, permission_profile="safe-edit"),
        "do it",
    )
    assert args[:5] == ["/usr/local/bin/agy", "--print", "do it", "--output-format", "json"]
    assert "--dangerously-skip-permissions" in args  # editing profile auto-approves in print mode


def test_resume_passes_conversation_id(tmp_path: Path):
    adapter = AntigravityAdapter(executable="agy")
    args = adapter._build_args(
        CliRunRequest(run_id="r", prompt="again", workspace=tmp_path, permission_profile="read-only"),
        "again", conversation="conv-123",
    )
    assert "--conversation" in args and "conv-123" in args
    assert args[args.index("--mode") + 1] == "plan"  # read-only -> plan mode, no edits


def test_registry_knows_antigravity():
    assert "antigravity" in known_cli_types()
    assert isinstance(build_cli_adapter("antigravity"), AntigravityAdapter)
    assert cli_display_name("antigravity") == "Antigravity"
    assert "Verified live" in cli_status_label("antigravity")


def test_not_installed_adapter_emits_single_failure(tmp_path: Path):
    adapter = AntigravityAdapter(executable="/nonexistent/agy")
    adapter.executable = None  # simulate "not installed" regardless of this host
    events = []

    async def _collect():
        async for e in adapter.start_run(
            CliRunRequest(run_id="r", prompt="x", workspace=tmp_path)
        ):
            events.append(e)

    import asyncio
    asyncio.run(_collect())
    assert _types(events) == ["run.failed"]
    assert events[0].data["error_type"] == "cli_not_found"


# --------------------------------------------------------------- end-to-end through a fake agy binary

@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shebang fake executable")
async def test_end_to_end_run_through_fake_agy(tmp_path: Path):
    """Drive the real adapter + run_managed_cli against a fake `agy` that prints the recorded result;
    exactly one terminal event (completed) must survive the finalizer."""
    fake = tmp_path / "agy"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"print(json.dumps({_real_result()!r}))\n",
        encoding="utf-8",
    )
    os.chmod(fake, 0o755)

    adapter = AntigravityAdapter(executable=str(fake))
    events = [
        e async for e in adapter.start_run(
            CliRunRequest(run_id="run_e2e", prompt="go", workspace=tmp_path)
        )
    ]
    types = _types(events)
    assert types[0] == "run.started"
    terminals = [t for t in types if t in ("run.completed", "run.failed", "run.cancelled")]
    assert terminals == ["run.completed"]
    assert "session.created" in types and "usage.updated" in types
