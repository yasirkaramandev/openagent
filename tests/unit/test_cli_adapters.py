"""Parser tests for the Codex and Claude CLI adapters (spec §7, §8, §40).

These parse recorded JSONL fixtures without invoking the real binary.
"""

import json
from pathlib import Path

from openagent.runtimes.cli.claude import map_claude_event
from openagent.runtimes.cli.codex import map_codex_event

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _events_from(fixture: str, mapper) -> list:
    events = []
    for line in (FIXTURES / fixture).read_text().splitlines():
        if line.strip():
            events.extend(mapper(json.loads(line), "run_1"))
    return events


def _types(events) -> list[str]:
    return [e.type for e in events]


# --------------------------------------------------------------------------- codex


def test_codex_session_and_completion():
    events = _events_from("codex_stream.jsonl", map_codex_event)
    types = _types(events)
    assert "session.created" in types
    assert "run.completed" in types
    session = next(e for e in events if e.type == "session.created")
    assert session.data["provider_session_id"] == "th-abc-123"


def test_codex_maps_command_and_files():
    events = _events_from("codex_stream.jsonl", map_codex_event)
    types = _types(events)
    assert "command.started" in types
    assert "command.completed" in types
    assert "file.created" in types   # kind "add"
    assert "file.modified" in types  # kind "update"
    paths = {e.data.get("path") for e in events if e.type.startswith("file.")}
    assert "tests/test_ws.py" in paths and "main.py" in paths


def test_codex_usage_parsed():
    events = _events_from("codex_stream.jsonl", map_codex_event)
    usage = next(e for e in events if e.type == "usage.updated")
    assert usage.data["input_tokens"] == 18000
    assert usage.data["output_tokens"] == 200


def test_codex_reasoning_not_surfaced():
    """Raw chain-of-thought must never appear in normalized events (spec §6)."""
    events = _events_from("codex_stream.jsonl", map_codex_event)
    blob = json.dumps([e.model_dump() for e in events])
    assert "internal chain of thought" not in blob


def test_codex_assistant_message():
    events = _events_from("codex_stream.jsonl", map_codex_event)
    msg = next(e for e in events if e.type == "message.completed")
    assert "WSS client" in msg.data["text"]


def test_codex_usage_limit_capture():
    """The real capture (usage limit) maps to a run.failed."""
    events = _events_from("codex_usage_limit.jsonl", map_codex_event)
    assert "run.failed" in _types(events)


# --------------------------------------------------------------------------- claude


def test_claude_session_and_result():
    events = _events_from("claude_stream.jsonl", map_claude_event)
    types = _types(events)
    assert "session.created" in types
    assert "run.completed" in types
    session = next(e for e in events if e.type == "session.created")
    assert session.data["provider_session_id"] == "sess-xyz-9"


def test_claude_text_deltas_and_tool():
    events = _events_from("claude_stream.jsonl", map_claude_event)
    types = _types(events)
    assert "message.delta" in types
    assert "tool.requested" in types
    tool = next(e for e in events if e.type == "tool.requested")
    assert tool.data["tool"] == "Edit"


def test_claude_usage_and_cost():
    events = _events_from("claude_stream.jsonl", map_claude_event)
    usage = next(e for e in events if e.type == "usage.updated")
    assert usage.data["input_tokens"] == 1200
    assert usage.data["cost_usd"] == 0.012
