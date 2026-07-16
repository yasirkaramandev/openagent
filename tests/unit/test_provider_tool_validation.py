from openagent.providers.base import normalized_tool_call


def test_tool_call_requires_id_and_name() -> None:
    event = normalized_tool_call(call_id=None, name="tool", arguments={})
    assert event.type == "error"
    assert event.error_type == "invalid_tool_call"


def test_tool_arguments_require_valid_object_json() -> None:
    malformed = normalized_tool_call(call_id="call_1", name="tool", arguments="{broken")
    scalar = normalized_tool_call(call_id="call_1", name="tool", arguments="[1, 2]")
    assert malformed.error_type == scalar.error_type == "invalid_tool_arguments"


def test_valid_tool_call_is_preserved() -> None:
    event = normalized_tool_call(call_id="call_1", name="tool", arguments='{"x": 1}')
    assert event.type == "tool_call"
    assert event.tool_call is not None
    assert event.tool_call.arguments == {"x": 1}
