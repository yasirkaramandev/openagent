"""Contract tests for the OpenAI Chat adapter (spec §40): the same battery every adapter must pass."""

import json

from pytest_httpx import HTTPXMock

from openagent.providers.base import Message, NormalizedModelRequest, Role, collect
from openagent.providers.openai_chat import OpenAIChatAdapter
from openagent.providers.transport import Transport

BASE = "https://api.test/v1"


def make_adapter(**kwargs) -> OpenAIChatAdapter:
    return OpenAIChatAdapter(base_url=BASE, api_key="sk-test", provider_type="deepseek", **kwargs)


def req(stream: bool = False, tools=None) -> NormalizedModelRequest:
    return NormalizedModelRequest(
        model="test-model", system="be brief",
        messages=[Message(role=Role.USER, content="hi")],
        tools=tools or [], stream=stream,
    )


async def test_text_response(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={
        "id": "chatcmpl-1",
        "choices": [{"message": {"role": "assistant", "content": "hello there"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })
    result = await collect(make_adapter().stream_response(req()))
    assert result.text == "hello there"
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5
    assert result.response_id == "chatcmpl-1"


async def test_tool_call_response(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={
        "id": "chatcmpl-2",
        "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "read_file", "arguments": json.dumps({"path": "main.py"})}},
        ]}}],
    })
    result = await collect(make_adapter().stream_response(req(tools=[{"name": "read_file"}])))
    assert result.has_tool_calls
    assert result.tool_calls[0].name == "read_file"
    assert result.tool_calls[0].arguments == {"path": "main.py"}


async def test_streaming_text_and_tool(httpx_mock: HTTPXMock):
    chunks = [
        {"id": "c1", "choices": [{"delta": {"content": "Hel"}}]},
        {"id": "c1", "choices": [{"delta": {"content": "lo"}}]},
        {"id": "c1", "choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_9", "function": {"name": "run_tests", "arguments": "{}"}}]}}]},
        {"id": "c1", "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    httpx_mock.add_response(content=body.encode(), headers={"content-type": "text/event-stream"})
    result = await collect(make_adapter().stream_response(req(stream=True, tools=[{"name": "run_tests"}])))
    assert result.text == "Hello"
    assert result.tool_calls[0].name == "run_tests"
    assert result.usage.output_tokens == 2


async def test_auth_error_maps_cleanly(httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=401, json={"error": {"message": "bad key"}})
    result = await collect(make_adapter().stream_response(req()))
    assert result.is_error
    assert result.error_type == "authentication_failed"


async def test_rate_limit_retries_then_errors(httpx_mock: HTTPXMock):
    for _ in range(3):
        httpx_mock.add_response(status_code=429, json={"error": {"message": "slow down"}})
    transport = Transport(base_url=BASE, headers={}, max_retries=2, backoff_base=0.0)
    result = await collect(make_adapter(transport=transport).stream_response(req()))
    assert result.is_error
    assert result.error_type == "provider_rate_limited"


async def test_malformed_response_is_safe(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"unexpected": "shape"})
    result = await collect(make_adapter().stream_response(req()))
    assert result.text == ""
    assert not result.has_tool_calls


async def test_payload_uses_compat_max_tokens_field(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"id": "x", "choices": [{"message": {"content": "ok"}}]})
    await collect(make_adapter().stream_response(req()))
    sent = json.loads(httpx_mock.get_requests()[0].content)
    # deepseek uses the standard max_tokens field
    assert "max_tokens" in sent
    assert sent["model"] == "test-model"
    assert sent["messages"][0]["role"] == "system"
