"""The shared OpenAI-Chat wire (spec §7, §8, §11).

Seven of the nine v0.2 providers speak this protocol. Written per-provider it becomes the same
subtly-different bug seven times, so it is written once and configured by a
:class:`CompatibilityProfile`. These tests pin the parts a provider can actually differ in — field
names, parameter bounds, how reasoning is asked for and returned, whether streamed tool arguments
need an opt-in — and the parts no provider is allowed to differ in: a fragment is never parsed, an
interrupted tool call is never silently dropped, and reasoning never leaks into assistant text.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from openagent.core.errors import ErrorType
from openagent.core.models import Protocol
from openagent.providers.base import Message, NormalizedModelRequest, Role
from openagent.providers.compat.profiles_v2 import (
    AssistantHistoryPolicy,
    CompatibilityProfile,
    ReasoningRequestStyle,
    get_profile,
)
from openagent.providers.streaming import FinishReason
from openagent.providers.transport import Transport
from openagent.providers.wire.openai_chat import OpenAIChatWire

BASE = "https://api.test/v1"


def wire(profile: CompatibilityProfile | None = None, **kwargs) -> OpenAIChatWire:
    return OpenAIChatWire(
        profile=profile or get_profile("deepseek"),
        transport=Transport(base_url=BASE, headers={"Content-Type": "application/json"}),
        **kwargs,
    )


def req(stream: bool = False, **kwargs) -> NormalizedModelRequest:
    fields = {
        "model": "test-model",
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


def sse(*chunks: dict) -> str:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return body + "data: [DONE]\n\n"


# --------------------------------------------------------------------------- payload shape


class TestPayloadFollowsTheProfile:
    def test_max_tokens_field_name_comes_from_the_profile(self) -> None:
        openai = wire(get_profile("openai"))
        payload, _ = openai.build_payload(req(), stream=False)
        assert payload["max_completion_tokens"] == 4096
        assert "max_tokens" not in payload

        deepseek = wire(get_profile("deepseek"))
        payload, _ = deepseek.build_payload(req(), stream=False)
        assert payload["max_tokens"] == 4096

    def test_temperature_is_clamped_to_the_profile_range(self) -> None:
        # MiniMax rejects temperature 0 outright, so the floor is 0.01 rather than 0.
        payload, _ = wire(get_profile("minimax")).build_payload(req(temperature=0.0), stream=False)
        assert payload["temperature"] == pytest.approx(0.01)

        payload, _ = wire(get_profile("kimi")).build_payload(req(temperature=1.9), stream=False)
        assert payload["temperature"] == pytest.approx(1.0)

    def test_dropped_params_never_reach_the_wire(self) -> None:
        profile = CompatibilityProfile(
            "x", drop_params=frozenset({"temperature"}), extra_request_fields={"n": 1}
        )
        payload, _ = wire(profile).build_payload(req(temperature=0.5), stream=False)
        assert "temperature" not in payload
        assert payload["n"] == 1

    def test_drop_params_wins_over_extra_request_fields(self) -> None:
        """Otherwise a profile could contradict itself and the outcome would depend on field order."""

        profile = CompatibilityProfile(
            "x", drop_params=frozenset({"n"}), extra_request_fields={"n": 4}
        )
        payload, _ = wire(profile).build_payload(req(), stream=False)
        assert "n" not in payload

    def test_stream_options_only_when_the_profile_says_usage_streams(self) -> None:
        payload, _ = wire(get_profile("deepseek")).build_payload(req(stream=True), stream=True)
        assert payload["stream_options"] == {"include_usage": True}

        payload, _ = wire(get_profile("nvidia-build")).build_payload(req(stream=True), stream=True)
        assert "stream_options" not in payload

    def test_non_stream_payload_carries_no_streaming_fields(self) -> None:
        payload, _ = wire().build_payload(req(), stream=False)
        assert "stream" not in payload
        assert "stream_options" not in payload


class TestToolChoiceAndToolStream:
    def test_required_degrades_to_auto_visibly_when_unsupported(self) -> None:
        payload, prep = wire(get_profile("kimi")).build_payload(
            req(tools=[PING_TOOL]), stream=False, tool_choice="required"
        )
        assert payload["tool_choice"] == "auto"
        assert prep.tool_choice_downgraded is True

    def test_supported_required_is_sent_unchanged(self) -> None:
        payload, prep = wire(get_profile("deepseek")).build_payload(
            req(tools=[PING_TOOL]), stream=False, tool_choice="required"
        )
        assert payload["tool_choice"] == "required"
        assert prep.tool_choice_downgraded is False

    def test_tool_stream_opt_in_is_sent_only_when_streaming_with_tools(self) -> None:
        glm = wire(get_profile("glm"))
        payload, _ = glm.build_payload(req(stream=True, tools=[PING_TOOL]), stream=True)
        assert payload["tool_stream"] is True

        payload, _ = glm.build_payload(req(stream=True), stream=True)
        assert "tool_stream" not in payload, "no tools means the opt-in has nothing to opt into"

        payload, _ = glm.build_payload(req(tools=[PING_TOOL]), stream=False)
        assert "tool_stream" not in payload, "a non-stream request has no tool stream"

    def test_a_profile_without_the_field_never_sends_it(self) -> None:
        payload, _ = wire(get_profile("deepseek")).build_payload(
            req(stream=True, tools=[PING_TOOL]), stream=True
        )
        assert "tool_stream" not in payload


class TestReasoningRequestStyles:
    def test_effort_style(self) -> None:
        payload, _ = wire(get_profile("openai"), reasoning_effort="high").build_payload(
            req(), stream=False
        )
        assert payload["reasoning"] == {"effort": "high"}

    def test_thinking_object_style(self) -> None:
        payload, _ = wire(get_profile("glm"), reasoning_effort="high").build_payload(
            req(), stream=False
        )
        assert payload["thinking"] == {"type": "enabled"}

    def test_thinking_object_style_can_be_disabled(self) -> None:
        payload, _ = wire(get_profile("glm"), reasoning_effort="off").build_payload(
            req(), stream=False
        )
        assert payload["thinking"] == {"type": "disabled"}

    def test_budget_style_carries_a_budget(self) -> None:
        profile = CompatibilityProfile(
            "x", reasoning_request_style=ReasoningRequestStyle.THINKING_BUDGET
        )
        payload, _ = wire(profile, reasoning_effort="medium", thinking_budget=2048).build_payload(
            req(), stream=False
        )
        assert payload["thinking"] == {"type": "enabled", "budget_tokens": 2048}

    def test_no_reasoning_field_when_the_profile_has_no_style(self) -> None:
        payload, _ = wire(get_profile("kimi"), reasoning_effort="high").build_payload(
            req(), stream=False
        )
        assert "reasoning" not in payload
        assert "thinking" not in payload

    def test_implicit_style_sends_nothing_even_with_an_effort(self) -> None:
        """The model reasons unconditionally; a request field would be rejected."""

        profile = CompatibilityProfile("x", reasoning_request_style=ReasoningRequestStyle.IMPLICIT)
        payload, _ = wire(profile, reasoning_effort="high").build_payload(req(), stream=False)
        assert "reasoning" not in payload and "thinking" not in payload


class TestToolNormalizationIsApplied:
    def test_an_unusable_tool_is_withheld_and_reported_not_sent(self) -> None:
        broken = {"name": "bad tool name!", "parameters": {"type": "object", "properties": {}}}
        payload, prep = wire().build_payload(req(tools=[broken, PING_TOOL]), stream=False)
        sent = [entry["function"]["name"] for entry in payload["tools"]]
        assert sent == ["ping"]
        assert prep.rejected == ["bad tool name!"]

    def test_all_tools_unusable_means_no_tools_field_at_all(self) -> None:
        broken = {"name": "", "parameters": {}}
        payload, prep = wire().build_payload(req(tools=[broken]), stream=False)
        assert "tools" not in payload
        assert "tool_choice" not in payload
        assert prep.rejected == ["<unnamed>"]

    def test_a_constraint_the_endpoint_cannot_express_is_recorded(self) -> None:
        profile = CompatibilityProfile("x", schema_unsupported_keywords=frozenset({"pattern"}))
        tool = {
            "name": "ping",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string", "pattern": "^a+$"}},
            },
        }
        _, prep = wire(profile).build_payload(req(tools=[tool]), stream=False)
        assert prep.narrows_validation is True
        assert any("pattern" in warning for warning in prep.warnings)


# --------------------------------------------------------------------------- streaming


class TestStreaming:
    async def test_text_deltas_are_yielded_as_they_arrive(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            text=sse(
                {"id": "c1", "choices": [{"delta": {"content": "he"}}]},
                {"id": "c1", "choices": [{"delta": {"content": "llo"}}]},
                {"id": "c1", "choices": [{"delta": {}, "finish_reason": "stop"}]},
            ),
            headers={"content-type": "text/event-stream"},
        )
        w = wire()
        texts = [e.text async for e in w.events(req(stream=True)) if e.type == "text_delta"]
        assert texts == ["he", "llo"]
        assert w.last_turn is not None
        assert w.last_turn.text == "hello"
        assert w.last_turn.finish_reason is FinishReason.STOP

    async def test_fragmented_tool_arguments_are_parsed_once_at_the_end(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            text=sse(
                {
                    "id": "c1",
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {"name": "ping", "arguments": '{"val'},
                                    }
                                ]
                            }
                        }
                    ],
                },
                {
                    "id": "c1",
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [{"index": 0, "function": {"arguments": 'ue": 1}'}}]
                            }
                        }
                    ],
                },
                {"id": "c1", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ),
            headers={"content-type": "text/event-stream"},
        )
        w = wire()
        events = [e async for e in w.events(req(stream=True, tools=[PING_TOOL]))]
        calls = [e.tool_call for e in events if e.type == "tool_call"]
        assert len(calls) == 1
        assert calls[0].name == "ping"
        assert calls[0].arguments == {"value": 1}
        assert not [e for e in events if e.type == "error"]

    async def test_parallel_calls_keep_their_order_and_identities(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            text=sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "a",
                                        "function": {"name": "ping", "arguments": "{}"},
                                    },
                                    {
                                        "index": 1,
                                        "id": "b",
                                        "function": {"name": "pong", "arguments": "{}"},
                                    },
                                ]
                            }
                        }
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ),
            headers={"content-type": "text/event-stream"},
        )
        w = wire()
        calls = [
            e.tool_call
            async for e in w.events(req(stream=True, tools=[PING_TOOL]))
            if e.type == "tool_call"
        ]
        assert [(c.id, c.name) for c in calls] == [("a", "ping"), ("b", "pong")]

    async def test_reasoning_is_captured_but_never_emitted_as_assistant_text(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            text=sse(
                {"choices": [{"delta": {"reasoning_content": "let me think"}}]},
                {"choices": [{"delta": {"content": "42"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ),
            headers={"content-type": "text/event-stream"},
        )
        w = wire(get_profile("deepseek"))
        events = [e async for e in w.events(req(stream=True))]
        assert "".join(e.text or "" for e in events if e.type == "text_delta") == "42"
        assert w.last_turn is not None
        assert w.last_turn.reasoning == "let me think"

    async def test_usage_from_the_final_chunk_is_reported(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            text=sse(
                {"choices": [{"delta": {"content": "x"}}]},
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 3,
                        "completion_tokens_details": {"reasoning_tokens": 2},
                    },
                },
            ),
            headers={"content-type": "text/event-stream"},
        )
        w = wire()
        usages = [e.usage async for e in w.events(req(stream=True)) if e.type == "usage"]
        assert len(usages) == 1
        assert usages[0].input_tokens == 11
        assert usages[0].output_tokens == 3
        assert usages[0].reasoning_tokens == 2

    async def test_a_tool_call_cut_off_mid_arguments_is_an_error_not_a_silent_drop(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """The whole point of the assembler: incomplete is a state, and it has to reach the caller."""

        httpx_mock.add_response(
            text=sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {"name": "ping", "arguments": '{"value": '},
                                    }
                                ]
                            }
                        }
                    ]
                },
            ),
            headers={"content-type": "text/event-stream"},
        )
        w = wire()
        events = [e async for e in w.events(req(stream=True, tools=[PING_TOOL]))]
        errors = [e for e in events if e.type == "error"]
        # The stream never sent a finish reason, so the *interruption* is the accurate diagnosis and
        # the half-written arguments are its symptom (spec §7.1). One error, not two to correlate —
        # and it still names the tool, which is what makes this a report rather than a silent drop.
        assert len(errors) == 1
        assert errors[0].error_type == ErrorType.STREAM_INTERRUPTED.value
        assert "ping" in (errors[0].error_message or "")
        assert not [e for e in events if e.type == "tool_call"]

    async def test_a_stream_that_ends_without_a_finish_reason_is_marked_interrupted(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            text=sse({"choices": [{"delta": {"content": "partial"}}]}),
            headers={"content-type": "text/event-stream"},
        )
        w = wire()
        [e async for e in w.events(req(stream=True))]
        assert w.last_turn is not None
        assert w.last_turn.finish_reason is FinishReason.INTERRUPTED

    async def test_keepalives_and_blank_events_do_not_invent_a_tool_call(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            text=(
                ": keep-alive\n\n"
                "\n"
                'data: {"choices": [{"delta": {"tool_calls": [{"function": {"arguments": ""}}]}}]}\n\n'
                'data: {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}\n\n'
                "data: [DONE]\n\n"
            ),
            headers={"content-type": "text/event-stream"},
        )
        w = wire()
        events = [e async for e in w.events(req(stream=True))]
        assert not [e for e in events if e.type == "tool_call"]
        assert not [e for e in events if e.type == "error"]


class TestNonStreaming:
    async def test_text_tool_calls_and_usage(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            json={
                "id": "c9",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "done",
                            "reasoning_content": "thought",
                            "tool_calls": [
                                {
                                    "id": "t1",
                                    "type": "function",
                                    "function": {"name": "ping", "arguments": '{"value": 2}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 6},
            }
        )
        w = wire(get_profile("deepseek"))
        events = [e async for e in w.events(req())]
        assert "".join(e.text or "" for e in events if e.type == "text_delta") == "done"
        calls = [e.tool_call for e in events if e.type == "tool_call"]
        assert calls[0].arguments == {"value": 2}
        assert w.last_turn is not None
        assert w.last_turn.reasoning == "thought"
        assert w.last_turn.response_id == "c9"

    async def test_the_provider_error_mapper_refines_the_status(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            status_code=402, json={"error": {"message": "Insufficient Balance"}}
        )
        w = wire(get_profile("deepseek"))
        errors = [e async for e in w.events(req()) if e.type == "error"]
        assert errors[0].error_type == ErrorType.INSUFFICIENT_BALANCE.value


# --------------------------------------------------------------------------- history / continuation


class TestAssistantHistory:
    def test_reasoning_is_replayed_when_the_profile_requires_it_for_tool_history(self) -> None:
        """DeepSeek wants reasoning_content back alongside the tool call it belongs to (spec §15.3).

        Dropping it does not error; it degrades the next turn. So the request built from history has
        to carry it, and that is exactly what this asserts.
        """

        history = req(
            messages=[
                Message(role=Role.USER, content="hi"),
                Message(
                    role=Role.ASSISTANT,
                    content="",
                    raw_blocks=[
                        {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "because",
                            "tool_calls": [
                                {
                                    "id": "t1",
                                    "type": "function",
                                    "function": {"name": "ping", "arguments": "{}"},
                                }
                            ],
                        }
                    ],
                ),
                Message(role=Role.TOOL, tool_call_id="t1", content="pong"),
            ]
        )
        payload, _ = wire(get_profile("deepseek")).build_payload(history, stream=False)
        assistant = [m for m in payload["messages"] if m["role"] == "assistant"][0]
        assert assistant["reasoning_content"] == "because"
        assert assistant["tool_calls"][0]["id"] == "t1"

    def test_a_normalized_text_profile_does_not_fabricate_a_reasoning_field(self) -> None:
        history = req(
            messages=[
                Message(role=Role.ASSISTANT, content="answer"),
            ]
        )
        payload, _ = wire(get_profile("kimi")).build_payload(history, stream=False)
        assistant = [m for m in payload["messages"] if m["role"] == "assistant"][0]
        assert "reasoning_content" not in assistant

    def test_a_tool_result_without_its_call_id_is_refused(self) -> None:
        """Sending it unkeyed attaches the answer to the wrong call, which nothing downstream can see."""

        history = req(messages=[Message(role=Role.TOOL, content="pong")])
        with pytest.raises(ValueError, match="call_id"):
            wire().build_payload(history, stream=False)


class TestContinuation:
    async def test_native_assistant_message_is_captured_for_replay(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            json={
                "id": "c1",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "why",
                            "tool_calls": [
                                {
                                    "id": "t1",
                                    "type": "function",
                                    "function": {"name": "ping", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ],
            }
        )
        w = wire(get_profile("deepseek"))
        [e async for e in w.events(req())]
        envelope = w.build_continuation(model_id="test-model")
        assert envelope.strategy.value == "native_message_replay"
        assert envelope.native_assistant_message is not None
        assert envelope.native_assistant_message["reasoning_content"] == "why"
        assert envelope.protocol is Protocol.OPENAI_CHAT

    async def test_a_text_only_turn_on_a_normalized_profile_falls_back_to_history(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            json={"id": "c1", "choices": [{"message": {"role": "assistant", "content": "hi"}}]}
        )
        w = wire(get_profile("kimi"))
        [e async for e in w.events(req())]
        assert w.build_continuation().strategy.value == "normalized_history"

    async def test_a_streamed_turn_reconstructs_a_replayable_native_message(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            text=sse(
                {"choices": [{"delta": {"reasoning_content": "hmm"}}]},
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "t1",
                                        "function": {"name": "ping", "arguments": "{}"},
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            ),
            headers={"content-type": "text/event-stream"},
        )
        profile = get_profile("minimax")
        assert (
            profile.assistant_history_policy is AssistantHistoryPolicy.NATIVE_MESSAGE_WITH_REASONING
        )
        w = OpenAIChatWire(
            profile=profile,
            transport=Transport(base_url=BASE, headers={}),
        )
        [e async for e in w.events(req(stream=True, tools=[PING_TOOL]))]
        envelope = w.build_continuation()
        assert envelope.native_assistant_message is not None
        native = envelope.native_assistant_message
        assert native["tool_calls"][0]["function"]["name"] == "ping"
        # MiniMax's profile names no reasoning response field, so no reasoning key is fabricated.
        assert "reasoning_content" not in native
