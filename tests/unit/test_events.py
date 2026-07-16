import json

from openagent.core.events import (
    EventType,
    ModelEventType,
    NormalizedEvent,
    NormalizedModelEvent,
    ToolCall,
)


def test_event_serializes_to_json_line():
    evt = NormalizedEvent(
        run_id="run_1",
        type=EventType.COMMAND_STARTED,
        source="codex-cli",
        data={"command": "pytest", "cwd": "/workspace"},
    )
    line = evt.to_json_line()
    parsed = json.loads(line)
    assert parsed["type"] == "command.started"
    assert parsed["run_id"] == "run_1"
    assert parsed["data"]["command"] == "pytest"
    assert parsed["id"].startswith("evt_")


def test_event_roundtrip():
    evt = NormalizedEvent(run_id="r", type=EventType.RUN_STARTED, source="openagent")
    again = NormalizedEvent.model_validate(json.loads(evt.to_json_line()))
    assert again.type == "run.started"
    assert again.run_id == "r"


def test_model_event_tool_call():
    evt = NormalizedModelEvent(
        type=ModelEventType.TOOL_CALL,
        tool_call=ToolCall(id="tc_1", name="read_file", arguments={"path": "main.py"}),
    )
    assert evt.tool_call is not None
    assert evt.tool_call.name == "read_file"
    assert evt.tool_call.arguments["path"] == "main.py"


def test_event_log_permissions(tmp_path):
    """events.jsonl is 0600 and its run dir 0700 on Unix, preserved across appends (item 13)."""

    import stat
    import sys

    from openagent.core.events import EventType, NormalizedEvent
    from openagent.storage.event_log import EventLog

    run_dir = tmp_path / "run_1"
    log = EventLog(run_dir)
    log.append(NormalizedEvent(run_id="r", type=EventType.RUN_STARTED, source="openagent"))

    events_path = run_dir / "events.jsonl"
    assert events_path.exists()
    if sys.platform.startswith("win"):
        return  # POSIX mode bits are not meaningful on Windows
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(events_path.stat().st_mode) == 0o600

    # A second append must not loosen the file mode.
    log.append(NormalizedEvent(run_id="r", type=EventType.RUN_COMPLETED, source="openagent"))
    assert stat.S_IMODE(events_path.stat().st_mode) == 0o600
    assert len(events_path.read_text().splitlines()) == 2
