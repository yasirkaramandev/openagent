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
    # Codex's reasoning_output_tokens is normalized onto reasoning_tokens (item 5). The tokens are
    # *counted*; the reasoning text they represent is never requested or stored.
    assert usage.data["reasoning_tokens"] == 320


def test_codex_reasoning_summary_is_surfaced():
    """Codex's ``reasoning`` item is the model's **summary**, and the user gets to see it (item 1).

    Confirmed live against codex-cli 0.142.5: a reasoning item carries a short, user-facing summary
    line (``"**Checking the WSS client before editing**"``), not raw chain-of-thought. Discarding it
    — as the adapter used to — left the user with no idea what the agent was doing.
    """

    events = _events_from("codex_stream.jsonl", map_codex_event)
    summary = next(e for e in events if e.type == "reasoning.summary")
    assert summary.data["text"] == "**Checking the WSS client before editing**"
    assert summary.data["item_id"] == "reason_1"       # addressable, so updates project onto it
    assert summary.data["status"] == "completed"


def test_codex_blank_reasoning_summary_is_dropped():
    """An empty summary is not an event — never render a blank 'Reasoning summary' card."""

    events = map_codex_event(
        {"type": "item.completed", "item": {"id": "r1", "type": "reasoning", "text": "   "}},
        "run_1",
    )
    assert events == []


def test_codex_undesignated_raw_fields_are_never_persisted():
    """Only text the backend designates as a *summary* is mapped; internals are dropped (item 22).

    A future/unknown Codex payload may carry raw provider internals alongside the summary. Anything
    not explicitly a reasoning summary must not reach a normalized event — not the encrypted
    reasoning blob, not raw content parts, not unknown internals.
    """

    events = map_codex_event(
        {"type": "item.completed", "item": {
            "id": "r2", "type": "reasoning", "text": "**Inspecting the parser**",
            "encrypted_content": "gAAAAA-secret-reasoning-blob",
            "raw_content": [{"type": "reasoning_text", "text": "step 1: I secretly think..."}],
            "summary_parts": ["hidden deliberation"],
        }},
        "run_1",
    )
    blob = json.dumps([e.model_dump() for e in events])
    assert "**Inspecting the parser**" in blob          # the designated summary is kept
    assert "gAAAAA-secret-reasoning-blob" not in blob   # …and nothing else is
    assert "I secretly think" not in blob
    assert "hidden deliberation" not in blob


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
    # Native total_cost_usd is normalized onto the single provider_cost field (item 12).
    assert usage.data["provider_cost"] == 0.012
    assert "cost_usd" not in usage.data


# ------------------------------------------------------- claude result success/failure (item 7)

def _result_type(obj: dict) -> str:
    events = map_claude_event({"type": "result", **obj}, "run_1")
    terminals = [e.type for e in events if e.type in ("run.completed", "run.failed")]
    assert len(terminals) == 1, f"expected exactly one terminal event, got {terminals}"
    return terminals[0]


def test_claude_result_success_subtype_completes():
    assert _result_type({"subtype": "success", "result": "ok", "is_error": False}) == "run.completed"


def test_claude_result_explicit_error_fails():
    assert _result_type(
        {"subtype": "error_during_execution", "is_error": True, "result": "boom"}
    ) == "run.failed"


def test_claude_result_missing_is_error_is_not_completed():
    # No subtype and no is_error field: ambiguous -> must NOT count as completed.
    assert _result_type({"result": "who knows"}) == "run.failed"


def test_claude_result_unknown_subtype_fails():
    assert _result_type({"subtype": "weird_new_state", "result": "?"}) == "run.failed"


def test_claude_result_malformed_fails():
    # A result object with nothing usable at all.
    assert _result_type({}) == "run.failed"


def test_claude_result_is_error_false_without_subtype_completes():
    assert _result_type({"is_error": False, "result": "done"}) == "run.completed"


# ----------------------------------------------- claude result: valid success envelope only (item 10)

def test_claude_result_is_error_false_without_result_fails():
    # is_error=false but no result string at all -> fail closed.
    assert _result_type({"is_error": False}) == "run.failed"


def test_claude_result_wrong_result_type_fails():
    # result must be a string; a dict/number is not a valid result envelope.
    assert _result_type({"is_error": False, "result": {"nested": 1}}) == "run.failed"
    assert _result_type({"subtype": "success", "result": 42}) == "run.failed"


def test_claude_result_conflicting_success_but_is_error_true_fails():
    assert _result_type({"subtype": "success", "is_error": True, "result": "ok"}) == "run.failed"


def test_claude_result_conflicting_error_subtype_but_is_error_false_fails():
    assert _result_type(
        {"subtype": "error_during_execution", "is_error": False, "result": "ok"}
    ) == "run.failed"


def test_claude_result_empty_object_fails():
    assert _result_type({}) == "run.failed"
