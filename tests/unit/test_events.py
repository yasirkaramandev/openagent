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
        run_id="run_1", type=EventType.COMMAND_STARTED, source="codex-cli",
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
