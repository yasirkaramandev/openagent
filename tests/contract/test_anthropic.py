"""Contract tests for the Anthropic Messages adapter (spec §40)."""

import json

import pytest
from pytest_httpx import HTTPXMock

from openagent.core.events import ToolCall
from openagent.providers.anthropic_messages import AnthropicMessagesAdapter, _to_anthropic_messages
from openagent.providers.base import Message, NormalizedModelRequest, Role, collect

BASE = "https://api.anthropic.test"


def make_adapter() -> AnthropicMessagesAdapter:
    return AnthropicMessagesAdapter(base_url=BASE, api_key="sk-ant-test")


def req(stream: bool = False, tools=None) -> NormalizedModelRequest:
    return NormalizedModelRequest(
        model="claude-x",
        system="be brief",
        messages=[Message(role=Role.USER, content="hi")],
        tools=tools or [],
        stream=stream,
    )


async def test_text_response(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json={
            "id": "msg_1",
            "content": [{"type": "text", "text": "hello"}],
            "usage": {"input_tokens": 8, "output_tokens": 3},
        }
    )
    result = await collect(make_adapter().stream_response(req()))
    assert result.text == "hello"
    assert result.usage.input_tokens == 8


async def test_tool_use_response(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json={
            "id": "msg_2",
            "content": [
                {"type": "text", "text": "let me look"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "apply_patch",
                    "input": {"path": "a.py", "old_string": "x", "new_string": "y"},
                },
            ],
            "usage": {"input_tokens": 20, "output_tokens": 10},
        }
    )
    result = await collect(make_adapter().stream_response(req(tools=[{"name": "apply_patch"}])))
    assert result.tool_calls[0].name == "apply_patch"
    assert result.tool_calls[0].arguments["path"] == "a.py"


async def test_streaming(httpx_mock: HTTPXMock):
    events = [
        {"type": "message_start", "message": {"id": "msg_3", "usage": {"input_tokens": 5}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi "}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "there"},
        },
        {"type": "message_delta", "usage": {"output_tokens": 4}},
        {"type": "message_stop"},
    ]
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    httpx_mock.add_response(content=body.encode(), headers={"content-type": "text/event-stream"})
    result = await collect(make_adapter().stream_response(req(stream=True)))
    assert result.text == "Hi there"
    assert result.usage.output_tokens == 4


async def test_auth_error(httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=401, json={"error": {"message": "invalid key"}})
    result = await collect(make_adapter().stream_response(req()))
    assert result.is_error
    assert result.error_type == "authentication_failed"


# --------------------------------------------------------------------------- health (item 10)


@pytest.mark.parametrize(
    ("status", "ok", "needle"),
    [
        (401, False, "authentication failed"),
        (403, False, "permission denied"),
        (402, False, "insufficient balance"),
        (503, False, "overloaded"),
        (400, False, "invalid configuration"),
        (429, True, "rate limited"),
        (404, True, "probe model not available"),
    ],
)
async def test_connection_classifies_errors(httpx_mock: HTTPXMock, status, ok, needle):
    httpx_mock.add_response(status_code=status, json={"error": {"message": "x"}})
    adapter = make_adapter()
    adapter.transport.max_retries = 0  # don't retry retryable statuses in the test
    result = await adapter.test_connection()
    assert result.ok is ok
    assert needle in result.detail


async def test_connection_reachable_on_success(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )
    result = await make_adapter().test_connection()
    assert result.ok is True
    assert result.detail == "reachable"


def test_raw_blocks_echo_hook_when_caller_sets_them():
    """The experimental raw_blocks hook echoes exact blocks *if a caller provides them* (item 11).

    This does not exercise end-to-end MiniMax fidelity — the API loop does not populate raw_blocks
    yet (see Message.raw_blocks). It only pins the echo behavior for the day it is wired up.
    """
    blocks = [
        {"type": "text", "text": "t"},
        {"type": "tool_use", "id": "u1", "name": "f", "input": {}},
    ]
    msg = Message(
        role=Role.ASSISTANT,
        raw_blocks=blocks,
        tool_calls=[ToolCall(id="u1", name="f", arguments={})],
    )
    out = _to_anthropic_messages([msg])
    assert out[0]["content"] == blocks
