"""The Responses, Ollama-native and LM-Studio-native wires (spec §12.2, §13.2, §13.3, §16.3).

Three properties here are worth more than the rest put together, because each one is a silent failure
if it is wrong:

* **``store`` is a privacy setting, and ``previous_response_id`` depends on it.** Sending the id while
  ``store`` is off is refused locally, because the provider's own error ("unknown response") names the
  wrong cause.
* **Ollama streams NDJSON.** An SSE reader drops every line and reports a successful empty turn.
* **Ollama has no tool-call ids and matches results by tool name.** A synthesized id therefore has to
  round-trip back to the name, or the second turn of every tool conversation is malformed.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from openagent.core.errors import ErrorType
from openagent.providers.base import Message, NormalizedModelRequest, Role
from openagent.providers.compat.profiles_v2 import CompatibilityProfile, get_profile
from openagent.providers.streaming import FinishReason
from openagent.providers.transport import Transport
from openagent.providers.wire.lmstudio_native import LmStudioNativeWire
from openagent.providers.wire.ollama_native import (
    OllamaNativeWire,
    synthesize_tool_id,
    tool_name_from_id,
)
from openagent.providers.wire.openai_responses import OpenAIResponsesWire

BASE = "http://localhost:1234"

PING_TOOL = {
    "name": "ping",
    "description": "probe",
    "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}},
}


def req(stream: bool = False, **kwargs) -> NormalizedModelRequest:
    fields = {
        "model": "m1",
        "system": "be brief",
        "messages": [Message(role=Role.USER, content="hi")],
        "stream": stream,
    }
    fields.update(kwargs)
    return NormalizedModelRequest(**fields)  # type: ignore[arg-type]


def ndjson(*objects: dict) -> str:
    return "".join(json.dumps(obj) + "\n" for obj in objects)


# =========================================================================== Responses


def responses(profile: CompatibilityProfile | None = None, **kwargs) -> OpenAIResponsesWire:
    return OpenAIResponsesWire(
        profile=profile or get_profile("lmstudio"),
        transport=Transport(base_url=BASE, headers={}),
        **kwargs,
    )


class TestResponsesServerState:
    def test_store_defaults_to_false_so_state_stays_local(self) -> None:
        payload, _ = responses().build_payload(req(), stream=False)
        assert payload["store"] is False

    def test_previous_response_id_without_store_is_refused_locally(self) -> None:
        wire = responses(store=False, previous_response_id="resp_1")
        with pytest.raises(ValueError, match="store=True"):
            wire.build_payload(req(), stream=False)

    def test_previous_response_id_is_sent_when_state_is_enabled(self) -> None:
        wire = responses(store=True, previous_response_id="resp_1")
        payload, _ = wire.build_payload(req(), stream=False)
        assert payload["previous_response_id"] == "resp_1"
        assert payload["store"] is True

    async def test_remote_continuation_only_when_the_provider_kept_it(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            json={"id": "resp_9", "status": "completed", "output": [], "usage": {}}
        )
        stored = responses(store=True)
        [e async for e in stored.events(req())]
        assert stored.build_continuation().strategy.value == "remote_id"

        httpx_mock.add_response(
            json={"id": "resp_9", "status": "completed", "output": [], "usage": {}}
        )
        local = responses(store=False)
        [e async for e in local.events(req())]
        assert local.build_continuation().strategy.value == "normalized_history"


class TestResponsesBody:
    def test_system_becomes_instructions(self) -> None:
        payload, _ = responses().build_payload(req(), stream=False)
        assert payload["instructions"] == "be brief"

    def test_tool_results_are_function_call_output_items(self) -> None:
        payload, _ = responses().build_payload(
            req(messages=[Message(role=Role.TOOL, tool_call_id="c1", content="pong")]),
            stream=False,
        )
        assert payload["input"][0] == {
            "type": "function_call_output",
            "call_id": "c1",
            "output": "pong",
        }

    def test_a_tool_result_without_a_call_id_is_refused(self) -> None:
        with pytest.raises(ValueError, match="call_id"):
            responses().build_payload(
                req(messages=[Message(role=Role.TOOL, content="pong")]), stream=False
            )

    async def test_output_text_and_function_calls(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            json={
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "hello"}]},
                    {
                        "type": "function_call",
                        "call_id": "fc_1",
                        "name": "ping",
                        "arguments": '{"value": 4}',
                    },
                ],
                "usage": {"input_tokens": 3, "output_tokens": 8},
            }
        )
        wire = responses()
        events = [e async for e in wire.events(req())]
        assert "".join(e.text or "" for e in events if e.type == "text_delta") == "hello"
        calls = [e.tool_call for e in events if e.type == "tool_call"]
        assert (calls[0].id, calls[0].arguments) == ("fc_1", {"value": 4})

    async def test_streamed_argument_deltas_are_parsed_once(self, httpx_mock: HTTPXMock) -> None:
        body = (
            'data: {"type": "response.created", "response": {"id": "resp_2"}}\n\n'
            'data: {"type": "response.output_item.added", "output_index": 0,'
            ' "item": {"type": "function_call", "call_id": "fc_2", "name": "ping"}}\n\n'
            'data: {"type": "response.function_call_arguments.delta", "output_index": 0,'
            ' "delta": "{\\"val"}\n\n'
            'data: {"type": "response.function_call_arguments.delta", "output_index": 0,'
            ' "delta": "ue\\": 7}"}\n\n'
            'data: {"type": "response.completed", "response": {"id": "resp_2",'
            ' "status": "completed", "usage": {"input_tokens": 1, "output_tokens": 2}}}\n\n'
        )
        httpx_mock.add_response(text=body, headers={"content-type": "text/event-stream"})
        wire = responses()
        calls = [
            e.tool_call
            async for e in wire.events(req(stream=True, tools=[PING_TOOL]))
            if e.type == "tool_call"
        ]
        assert calls[0].arguments == {"value": 7}

    async def test_an_incomplete_response_is_reported_as_length_limited(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            json={
                "id": "r",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "cut"}]}
                ],
            }
        )
        wire = responses()
        [e async for e in wire.events(req())]
        assert wire.last_turn is not None
        assert wire.last_turn.finish_reason is FinishReason.LENGTH


# =========================================================================== Ollama


def ollama(**kwargs) -> OllamaNativeWire:
    return OllamaNativeWire(
        profile=get_profile("ollama"),
        transport=Transport(base_url="http://localhost:11434", headers={}),
        **kwargs,
    )


class TestOllamaToolIdentity:
    def test_a_synthesized_id_round_trips_to_the_tool_name(self) -> None:
        assert tool_name_from_id(synthesize_tool_id(0, "read_file")) == "read_file"

    def test_a_foreign_id_is_not_claimed(self) -> None:
        assert tool_name_from_id("call_abc123") is None

    def test_a_tool_result_is_keyed_by_name_recovered_from_the_id(self) -> None:
        payload, _ = ollama().build_payload(
            req(
                messages=[
                    Message(
                        role=Role.TOOL,
                        tool_call_id=synthesize_tool_id(0, "ping"),
                        content="pong",
                    )
                ]
            ),
            stream=False,
        )
        assert payload["messages"][1] == {"role": "tool", "content": "pong", "tool_name": "ping"}

    def test_a_result_with_neither_a_recoverable_id_nor_a_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="tool name"):
            ollama().build_payload(
                req(messages=[Message(role=Role.TOOL, tool_call_id="call_x", content="p")]),
                stream=False,
            )


class TestOllamaPayload:
    def test_max_tokens_and_temperature_go_into_options(self) -> None:
        payload, _ = ollama().build_payload(req(temperature=0.4), stream=False)
        assert payload["options"] == {"num_predict": 4096, "temperature": pytest.approx(0.4)}

    def test_think_levels_are_passed_through_only_when_documented(self) -> None:
        assert (
            ollama(reasoning_effort="high").build_payload(req(), stream=False)[0]["think"] == "high"
        )
        # An unrecognised effort becomes a plain enable rather than a value the server may reject.
        assert (
            ollama(reasoning_effort="ultra").build_payload(req(), stream=False)[0]["think"] is True
        )
        assert (
            ollama(reasoning_effort="off").build_payload(req(), stream=False)[0]["think"] is False
        )

    def test_no_think_field_when_no_effort_was_requested(self) -> None:
        payload, _ = ollama().build_payload(req(), stream=False)
        assert "think" not in payload

    def test_keep_alive_is_only_sent_when_the_caller_chose_one(self) -> None:
        assert "keep_alive" not in ollama().build_payload(req(), stream=False)[0]
        assert ollama(keep_alive="5m").build_payload(req(), stream=False)[0]["keep_alive"] == "5m"

    def test_requesting_a_guaranteed_tool_call_is_reported_as_a_downgrade(self) -> None:
        """Ollama documents no tool_choice, so "required" cannot be honoured — and says so."""

        _, prep = ollama().build_payload(
            req(tools=[PING_TOOL]), stream=False, tool_choice="required"
        )
        assert prep.tool_choice_downgraded is True


class TestOllamaStreaming:
    async def test_ndjson_lines_are_read(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            text=ndjson(
                {"message": {"role": "assistant", "content": "he"}, "done": False},
                {"message": {"role": "assistant", "content": "llo"}, "done": False},
                {
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 12,
                    "eval_count": 4,
                },
            )
        )
        wire = ollama()
        events = [e async for e in wire.events(req(stream=True))]
        assert "".join(e.text or "" for e in events if e.type == "text_delta") == "hello"
        usage = [e.usage for e in events if e.type == "usage"][0]
        assert (usage.input_tokens, usage.output_tokens) == (12, 4)
        assert wire.last_turn is not None
        assert wire.last_turn.finish_reason is FinishReason.STOP

    async def test_thinking_is_kept_separate_from_content(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            text=ndjson(
                {
                    "message": {"role": "assistant", "content": "", "thinking": "hmm "},
                    "done": False,
                },
                {"message": {"role": "assistant", "content": "42"}, "done": False},
                {
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "done_reason": "stop",
                },
            )
        )
        wire = ollama()
        events = [e async for e in wire.events(req(stream=True))]
        assert "".join(e.text or "" for e in events if e.type == "text_delta") == "42"
        assert wire.last_turn is not None
        assert wire.last_turn.reasoning == "hmm "

    async def test_a_stream_with_no_done_marker_is_interrupted(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            text=ndjson({"message": {"role": "assistant", "content": "partial"}, "done": False})
        )
        wire = ollama()
        events = [e async for e in wire.events(req(stream=True))]
        assert wire.last_turn is not None
        assert wire.last_turn.finish_reason is FinishReason.INTERRUPTED
        assert [e for e in events if e.error_type == ErrorType.STREAM_INTERRUPTED.value]

    async def test_a_load_only_response_is_not_a_completed_turn(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """``done_reason: "load"`` means the model was loaded and nothing was generated."""

        httpx_mock.add_response(
            json={
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": "load",
            }
        )
        wire = ollama()
        [e async for e in wire.events(req())]
        assert wire.last_turn is not None
        assert wire.last_turn.finish_reason is FinishReason.INTERRUPTED

    async def test_tool_calls_get_ids_and_a_replayable_native_message(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            json={
                "message": {
                    "role": "assistant",
                    "content": "",
                    "thinking": "need the file",
                    "tool_calls": [
                        {"function": {"name": "ping", "arguments": {"value": 1}}},
                    ],
                },
                "done": True,
                "done_reason": "stop",
            }
        )
        wire = ollama()
        events = [e async for e in wire.events(req(tools=[PING_TOOL]))]
        calls = [e.tool_call for e in events if e.type == "tool_call"]
        assert calls[0].name == "ping"
        assert tool_name_from_id(calls[0].id) == "ping"
        envelope = wire.build_continuation(model_id="m1")
        assert envelope.strategy.value == "native_message_replay"
        assert envelope.native_assistant_message["thinking"] == "need the file"

    async def test_a_plain_text_turn_needs_no_native_artifact(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            json={
                "message": {"role": "assistant", "content": "hi"},
                "done": True,
                "done_reason": "stop",
            }
        )
        wire = ollama()
        [e async for e in wire.events(req())]
        assert wire.build_continuation().strategy.value == "normalized_history"

    async def test_a_local_connection_failure_is_reported_as_the_server_being_down(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """A refused connection means "start your server", not "wait" (spec §12.4).

        Retries are disabled here so the assertion is about *classification*. That a network failure
        is retried at all is covered separately — mixing the two would let a retry-count change
        silently rewrite what this test proves.
        """

        import httpx

        httpx_mock.add_exception(httpx.ConnectError("All connection attempts failed"))
        wire = OllamaNativeWire(
            profile=get_profile("ollama"),
            transport=Transport(base_url="http://localhost:11434", headers={}, max_retries=0),
        )
        errors = [e async for e in wire.events(req()) if e.type == "error"]
        assert errors[0].error_type == ErrorType.LOCAL_SERVER_UNAVAILABLE.value

    async def test_a_read_timeout_is_not_reclassified_as_a_down_server(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """A slow local model is a different problem with a different remedy."""

        import httpx

        httpx_mock.add_exception(httpx.ReadTimeout("timed out"))
        wire = OllamaNativeWire(
            profile=get_profile("ollama"),
            transport=Transport(base_url="http://localhost:11434", headers={}, max_retries=0),
        )
        errors = [e async for e in wire.events(req()) if e.type == "error"]
        assert errors[0].error_type == ErrorType.TIMEOUT.value


# =========================================================================== LM Studio native


class TestLmStudioNative:
    def test_it_targets_the_v0_path_and_reuses_the_chat_serializer(self) -> None:
        wire = LmStudioNativeWire(
            profile=get_profile("lmstudio"), transport=Transport(base_url=BASE, headers={})
        )
        assert wire.path == "/api/v0/chat/completions"
        payload, _ = wire.build_payload(req(), stream=False)
        # Same body the chat wire builds — the path is the only difference.
        assert payload["messages"][0]["role"] == "system"

    async def test_stats_and_model_info_are_captured_for_diagnostics(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            json={
                "id": "c1",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "stats": {"tokens_per_second": 41.2, "time_to_first_token": 0.12},
                "model_info": {"arch": "qwen3", "quantization": "Q4_K_M"},
            }
        )
        wire = LmStudioNativeWire(
            profile=get_profile("lmstudio"), transport=Transport(base_url=BASE, headers={})
        )
        [e async for e in wire.events(req())]
        assert wire.last_stats["tokens_per_second"] == pytest.approx(41.2)
        assert wire.model_revision == "qwen3/Q4_K_M"

    def test_an_unreported_model_identity_stays_absent_rather_than_invented(self) -> None:
        wire = LmStudioNativeWire(
            profile=get_profile("lmstudio"), transport=Transport(base_url=BASE, headers={})
        )
        assert wire.model_revision is None
