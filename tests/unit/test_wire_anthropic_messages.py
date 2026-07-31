"""The shared Anthropic Messages wire (spec §7, §19).

MiniMax's first-class protocol and one of Qwen's and LM Studio's options. The property that makes
this wire different from the chat wire is **block fidelity**: an assistant turn is an ordered list of
typed content blocks, thinking blocks carry signatures, and a continuation that flattens them into
text produces a request the provider accepts and answers worse. So the block list is preserved whole,
in order, and the tests below pin that rather than trusting it.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from openagent.core.errors import ErrorType
from openagent.core.models import Protocol
from openagent.providers.base import Message, NormalizedModelRequest, Role
from openagent.providers.compat.profiles_v2 import CompatibilityProfile, get_profile
from openagent.providers.streaming import FinishReason
from openagent.providers.transport import Transport
from openagent.providers.wire.anthropic_messages import AnthropicMessagesWire

BASE = "https://api.test"


def wire(profile: CompatibilityProfile | None = None, **kwargs) -> AnthropicMessagesWire:
    return AnthropicMessagesWire(
        profile=profile or get_profile("minimax"),
        transport=Transport(base_url=BASE, headers={}),
        **kwargs,
    )


def req(stream: bool = False, **kwargs) -> NormalizedModelRequest:
    fields = {
        "model": "m1",
        "system": "be brief",
        "messages": [Message(role=Role.USER, content="hi")],
        "stream": stream,
    }
    fields.update(kwargs)
    return NormalizedModelRequest(**fields)  # type: ignore[arg-type]


PING_TOOL = {
    "name": "ping",
    "description": "probe",
    "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}},
}


def sse(*events: tuple[str, dict]) -> str:
    return "".join(f"event: {name}\ndata: {json.dumps(body)}\n\n" for name, body in events)


class TestPayload:
    def test_system_is_a_top_level_field_not_a_message(self) -> None:
        payload, _ = wire().build_payload(req(), stream=False)
        assert payload["system"] == "be brief"
        assert all(m["role"] != "system" for m in payload["messages"])

    def test_max_tokens_is_required_and_always_sent(self) -> None:
        payload, _ = wire().build_payload(req(), stream=False)
        assert payload["max_tokens"] == 4096

    def test_tools_use_input_schema_not_parameters(self) -> None:
        payload, _ = wire().build_payload(req(tools=[PING_TOOL]), stream=False)
        tool = payload["tools"][0]
        assert tool["name"] == "ping"
        assert tool["input_schema"]["type"] == "object"
        assert "parameters" not in tool

    def test_tool_choice_uses_anthropics_own_vocabulary(self) -> None:
        payload, _ = wire().build_payload(req(tools=[PING_TOOL]), stream=False, tool_choice="auto")
        assert payload["tool_choice"] == {"type": "auto"}

    def test_required_becomes_any_when_the_endpoint_supports_it(self) -> None:
        profile = CompatibilityProfile("x", supports_tool_choice_required=True)
        payload, prep = wire(profile).build_payload(
            req(tools=[PING_TOOL]), stream=False, tool_choice="required"
        )
        assert payload["tool_choice"] == {"type": "any"}
        assert prep.tool_choice_downgraded is False

    def test_temperature_is_clamped_to_the_anthropic_range(self) -> None:
        payload, _ = wire(get_profile("anthropic")).build_payload(
            req(temperature=1.8), stream=False
        )
        assert payload["temperature"] == pytest.approx(1.0)

    def test_tool_results_become_user_content_blocks(self) -> None:
        payload, _ = wire().build_payload(
            req(
                messages=[
                    Message(role=Role.USER, content="hi"),
                    Message(role=Role.TOOL, tool_call_id="t1", content="pong"),
                ]
            ),
            stream=False,
        )
        last = payload["messages"][-1]
        assert last["role"] == "user"
        assert last["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": "pong"}

    def test_consecutive_tool_results_merge_into_one_user_message(self) -> None:
        """Two user messages in a row is a shape Anthropic rejects."""

        payload, _ = wire().build_payload(
            req(
                messages=[
                    Message(role=Role.TOOL, tool_call_id="a", content="1"),
                    Message(role=Role.TOOL, tool_call_id="b", content="2"),
                ]
            ),
            stream=False,
        )
        assert len(payload["messages"]) == 1
        assert len(payload["messages"][0]["content"]) == 2

    def test_a_tool_result_without_its_id_is_refused(self) -> None:
        with pytest.raises(ValueError, match="tool_use_id"):
            wire().build_payload(req(messages=[Message(role=Role.TOOL, content="x")]), stream=False)

    def test_assistant_native_blocks_are_replayed_verbatim(self) -> None:
        blocks = [
            {"type": "thinking", "thinking": "hmm", "signature": "sig-1"},
            {"type": "tool_use", "id": "t1", "name": "ping", "input": {}},
        ]
        payload, _ = wire().build_payload(
            req(messages=[Message(role=Role.ASSISTANT, content="", raw_blocks=blocks)]),
            stream=False,
        )
        assert payload["messages"][0] == {"role": "assistant", "content": blocks}


class TestNonStreaming:
    async def test_text_thinking_and_tool_use_blocks(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            json={
                "id": "msg_1",
                "content": [
                    {"type": "thinking", "thinking": "let me see", "signature": "sig"},
                    {"type": "text", "text": "answer"},
                    {"type": "tool_use", "id": "t1", "name": "ping", "input": {"value": 3}},
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 7, "output_tokens": 9},
            }
        )
        w = wire()
        events = [e async for e in w.events(req())]
        assert "".join(e.text or "" for e in events if e.type == "text_delta") == "answer"
        calls = [e.tool_call for e in events if e.type == "tool_call"]
        assert (calls[0].id, calls[0].name, calls[0].arguments) == ("t1", "ping", {"value": 3})
        assert w.last_turn is not None
        assert w.last_turn.reasoning == "let me see"
        assert w.last_turn.finish_reason is FinishReason.TOOL_CALLS

    async def test_usage_includes_cache_reads(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            json={
                "id": "m",
                "content": [{"type": "text", "text": "x"}],
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 40,
                },
            }
        )
        usages = [e.usage async for e in wire().events(req()) if e.type == "usage"]
        assert usages[0].cached_input_tokens == 40

    async def test_native_blocks_are_kept_whole_and_in_order(self, httpx_mock: HTTPXMock) -> None:
        content = [
            {"type": "thinking", "thinking": "a", "signature": "s"},
            {"type": "text", "text": "b"},
            {"type": "tool_use", "id": "t", "name": "ping", "input": {}},
        ]
        httpx_mock.add_response(json={"id": "m", "content": content, "stop_reason": "tool_use"})
        w = wire()
        [e async for e in w.events(req())]
        envelope = w.build_continuation(model_id="m1")
        assert envelope.strategy.value == "native_message_replay"
        assert envelope.native_assistant_message == {"role": "assistant", "content": content}
        assert envelope.protocol is Protocol.ANTHROPIC_MESSAGES

    async def test_reasoning_never_reaches_assistant_text(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            json={
                "id": "m",
                "content": [
                    {"type": "thinking", "thinking": "SECRET-REASONING"},
                    {"type": "text", "text": "visible"},
                ],
            }
        )
        events = [e async for e in wire().events(req())]
        text = "".join(e.text or "" for e in events if e.type == "text_delta")
        assert text == "visible"
        assert "SECRET-REASONING" not in text


class TestStreaming:
    async def test_text_and_tool_input_deltas(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            text=sse(
                ("message_start", {"type": "message_start", "message": {"id": "msg_9"}}),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "hel"},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "lo"},
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 1,
                        "content_block": {"type": "tool_use", "id": "t1", "name": "ping"},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 1,
                        "delta": {"type": "input_json_delta", "partial_json": '{"val'},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 1,
                        "delta": {"type": "input_json_delta", "partial_json": 'ue": 5}'},
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 1}),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "tool_use"},
                        "usage": {"output_tokens": 12},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ),
            headers={"content-type": "text/event-stream"},
        )
        w = wire()
        events = [e async for e in w.events(req(stream=True, tools=[PING_TOOL]))]
        assert "".join(e.text or "" for e in events if e.type == "text_delta") == "hello"
        calls = [e.tool_call for e in events if e.type == "tool_call"]
        assert calls[0].arguments == {"value": 5}
        assert w.last_turn is not None
        assert w.last_turn.response_id == "msg_9"

    async def test_interleaved_thinking_is_captured_with_its_signature(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            text=sse(
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "thinking", "thinking": ""},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "thinking_delta", "thinking": "step "},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "signature_delta", "signature": "sig-abc"},
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                ),
            ),
            headers={"content-type": "text/event-stream"},
        )
        w = wire()
        [e async for e in w.events(req(stream=True))]
        assert w.last_turn is not None
        assert w.last_turn.reasoning == "step "
        native = w.native_assistant_message
        assert native is not None
        thinking = [b for b in native["content"] if b["type"] == "thinking"][0]
        assert thinking["signature"] == "sig-abc", (
            "a thinking block replayed without its signature is rejected"
        )

    async def test_a_stream_error_event_becomes_an_error(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            text=sse(
                (
                    "error",
                    {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
                )
            ),
            headers={"content-type": "text/event-stream"},
        )
        errors = [e async for e in wire().events(req(stream=True)) if e.type == "error"]
        assert errors[0].error_type == ErrorType.PROVIDER_OVERLOADED.value

    async def test_a_truncated_tool_input_is_reported_not_executed(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            text=sse(
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "tool_use", "id": "t1", "name": "ping"},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "input_json_delta", "partial_json": '{"value"'},
                    },
                ),
            ),
            headers={"content-type": "text/event-stream"},
        )
        events = [e async for e in wire().events(req(stream=True, tools=[PING_TOOL]))]
        assert not [e for e in events if e.type == "tool_call"]
        assert [e for e in events if e.type == "error"]

    async def test_ping_events_are_ignored(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            text=sse(
                ("ping", {"type": "ping"}),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "ok"},
                    },
                ),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
            ),
            headers={"content-type": "text/event-stream"},
        )
        w = wire()
        events = [e async for e in w.events(req(stream=True))]
        assert "".join(e.text or "" for e in events if e.type == "text_delta") == "ok"
        assert not [e for e in events if e.type == "error"]
