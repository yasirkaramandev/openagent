"""Contract tests for the OpenAI Responses adapter (spec §12, §40)."""

import json

from pytest_httpx import HTTPXMock

from openagent.providers.base import Message, NormalizedModelRequest, Role, collect
from openagent.providers.openai_responses import OpenAIResponsesAdapter

BASE = "https://api.openai.test/v1"


def make_adapter() -> OpenAIResponsesAdapter:
    return OpenAIResponsesAdapter(base_url=BASE, api_key="sk-test")


def req(stream: bool = False, tools=None) -> NormalizedModelRequest:
    return NormalizedModelRequest(
        model="gpt-x",
        system="be brief",
        messages=[Message(role=Role.USER, content="hi")],
        tools=tools or [],
        stream=stream,
    )


async def test_text_output(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json={
            "id": "resp_1",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}],
            "usage": {"input_tokens": 6, "output_tokens": 2},
        }
    )
    result = await collect(make_adapter().stream_response(req()))
    assert result.text == "hello"
    assert result.response_id == "resp_1"
    assert result.usage.output_tokens == 2


async def test_function_call_output(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json={
            "id": "resp_2",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "fc_1",
                    "name": "search_text",
                    "arguments": json.dumps({"query": "TODO"}),
                },
            ],
        }
    )
    result = await collect(make_adapter().stream_response(req(tools=[{"name": "search_text"}])))
    assert result.tool_calls[0].name == "search_text"
    assert result.tool_calls[0].arguments == {"query": "TODO"}
    assert result.tool_calls[0].id == "fc_1"


async def test_tool_result_roundtrip_payload(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json={
            "id": "r",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}],
        }
    )
    request = NormalizedModelRequest(
        model="gpt-x",
        messages=[
            Message(role=Role.USER, content="edit"),
            Message(role=Role.TOOL, tool_call_id="fc_1", content="patched"),
        ],
    )
    await collect(make_adapter().stream_response(request))
    sent = json.loads(httpx_mock.get_requests()[0].content)
    kinds = [i.get("type") or i.get("role") for i in sent["input"]]
    assert "function_call_output" in kinds


async def test_error(httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=403, json={"error": {"message": "no access"}})
    result = await collect(make_adapter().stream_response(req()))
    assert result.is_error
    assert result.error_type == "permission_denied"


# --------------------------------------------------------------------------- incomplete (item 8)


async def test_incomplete_max_tokens_is_context_limit(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json={
            "id": "resp_i",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "partial"}]}
            ],
            "usage": {"input_tokens": 6, "output_tokens": 16},
        }
    )
    result = await collect(make_adapter().stream_response(req()))
    assert result.is_error
    assert result.error_type == "context_limit"


async def test_incomplete_content_filter_maps_to_content_filtered(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json={
            "id": "resp_i2",
            "status": "incomplete",
            "incomplete_details": {"reason": "content_filter"},
            "output": [],
        }
    )
    result = await collect(make_adapter().stream_response(req()))
    assert result.error_type == "content_filtered"


async def test_incomplete_other_reason_is_incomplete_response(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json={
            "id": "resp_i3",
            "status": "incomplete",
            "incomplete_details": {"reason": "something_else"},
            "output": [],
        }
    )
    result = await collect(make_adapter().stream_response(req()))
    assert result.error_type == "incomplete_response"


async def test_incomplete_does_not_surface_tool_calls(httpx_mock: HTTPXMock):
    # A truncated response may contain a partial function_call — it must not be executed.
    httpx_mock.add_response(
        json={
            "id": "resp_i4",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [
                {
                    "type": "function_call",
                    "call_id": "fc_x",
                    "name": "run_command",
                    "arguments": "{",
                }
            ],
        }
    )
    result = await collect(make_adapter().stream_response(req(tools=[{"name": "run_command"}])))
    assert result.is_error
    assert result.tool_calls == []


async def test_incomplete_streaming_is_error_not_done(httpx_mock: HTTPXMock):
    sse = (
        'data: {"type":"response.created","response":{"id":"resp_s"}}\n\n'
        'data: {"type":"response.output_text.delta","delta":"par"}\n\n'
        'data: {"type":"response.incomplete","response":{"id":"resp_s","status":"incomplete",'
        '"incomplete_details":{"reason":"max_output_tokens"}}}\n\n'
    )
    httpx_mock.add_response(text=sse, headers={"content-type": "text/event-stream"})
    result = await collect(make_adapter().stream_response(req(stream=True)))
    assert result.is_error
    assert result.error_type == "context_limit"
