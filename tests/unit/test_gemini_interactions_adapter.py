"""Gemini Interactions adapter (spec §10, §27).

Offline contract tests against fixture streams. These prove the adapter's *mapping* is right; they
cannot prove Google's wire format is what the documentation says, which is what the live smoke is
for. Nothing here should be read as live verification.
"""

from __future__ import annotations

import json

import pytest

from openagent.core.errors import ErrorType
from openagent.core.models import Protocol
from openagent.providers.base import (
    Message,
    ModelCatalogError,
    NormalizedModelRequest,
    Role,
    collect,
)
from openagent.providers.continuation import ContinuationStrategy
from openagent.providers.gemini_interactions import GeminiInteractionsAdapter
from openagent.providers.transport import Transport, TransportError

pytestmark = pytest.mark.unit


class _FakeTransport(Transport):
    """A transport that replays scripted SSE events and JSON bodies."""

    def __init__(self, *, events=None, post=None, gets=None) -> None:
        super().__init__(base_url="https://gemini.test")
        self._events = events or []
        self._post = post or {}
        self._gets = list(gets or [])
        self.posted: list[dict] = []
        self.paths: list[str] = []

    async def stream_sse(self, path, payload):  # type: ignore[override]
        self.paths.append(path)
        self.posted.append(payload)
        for event in self._events:
            if isinstance(event, Exception):
                raise event
            yield event

    async def post_json(self, path, payload):  # type: ignore[override]
        self.paths.append(path)
        self.posted.append(payload)
        if isinstance(self._post, Exception):
            raise self._post
        return self._post

    async def get_json(self, path):  # type: ignore[override]
        self.paths.append(path)
        item = self._gets.pop(0) if self._gets else {}
        if isinstance(item, Exception):
            raise item
        return item


def _adapter(**kwargs) -> GeminiInteractionsAdapter:
    transport = kwargs.pop("transport", None) or _FakeTransport()
    return GeminiInteractionsAdapter(api_key="test-key", transport=transport, **kwargs)


def _request(**kwargs) -> NormalizedModelRequest:
    defaults = {
        "model": "gemini-3.6-flash",
        "messages": [Message(role=Role.USER, content="hi")],
        "stream": True,
    }
    defaults.update(kwargs)
    return NormalizedModelRequest(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- auth


def test_the_api_key_travels_in_a_header_not_the_url():
    # A key in a query string reaches proxy logs and crash reports; a header does not.
    adapter = GeminiInteractionsAdapter(api_key="secret-key")

    assert adapter.transport.headers["x-goog-api-key"] == "secret-key"
    assert "secret-key" not in adapter.transport.base_url


def test_no_api_key_sends_no_auth_header():
    adapter = GeminiInteractionsAdapter(api_key=None)

    assert "x-goog-api-key" not in adapter.transport.headers


# --------------------------------------------------------------------------- payload


def test_payload_carries_the_documented_fields():
    adapter = _adapter()
    payload = adapter.build_payload(_request(system="be brief", max_tokens=256, temperature=0.4))

    assert payload["model"] == "gemini-3.6-flash"
    assert payload["system_instruction"] == "be brief"
    assert payload["generation_config"] == {"max_output_tokens": 256, "temperature": 0.4}
    assert payload["input"] == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_local_only_state_is_the_default():
    # Server-side retention is a privacy decision the user makes knowingly (spec §10.4).
    assert _adapter().build_payload(_request())["store"] is False


def test_store_true_is_carried_when_the_user_opted_in():
    assert _adapter(store=True).build_payload(_request())["store"] is True


def test_tools_get_the_function_type_discriminator():
    tool = {"name": "read", "description": "read a file", "parameters": {"type": "object"}}
    payload = _adapter().build_payload(_request(tools=[tool]))

    assert payload["tools"] == [
        {
            "type": "function",
            "name": "read",
            "description": "read a file",
            "parameters": {"type": "object"},
        }
    ]


def test_a_tool_result_becomes_a_function_result_keyed_by_call_id():
    messages = [
        Message(role=Role.USER, content="go"),
        Message(role=Role.TOOL, content="done", tool_call_id="call_7", name="read"),
    ]
    payload = _adapter().build_payload(_request(messages=messages))

    assert payload["input"][1] == {
        "type": "function_result",
        "name": "read",
        "call_id": "call_7",
        "result": [{"type": "text", "text": "done"}],
    }


def test_a_tool_result_without_a_call_id_is_refused():
    # Sending it unkeyed would attach the result to whichever call the provider guesses.
    messages = [Message(role=Role.TOOL, content="done", name="read")]

    with pytest.raises(ValueError, match="call_id"):
        _adapter().build_payload(_request(messages=messages))


def test_native_steps_are_replayed_verbatim_when_present():
    steps = [{"type": "thought", "thought_signature": "sig-abc"}]
    messages = [Message(role=Role.ASSISTANT, content="ignored", raw_blocks=steps)]
    payload = _adapter().build_payload(_request(messages=messages))

    assert payload["input"] == steps


def test_previous_interaction_id_requires_server_side_state():
    # The provider can only look up an interaction it was asked to keep; the real cause of the
    # failure is local, so it is raised locally rather than surfaced as a confusing provider error.
    adapter = _adapter(store=False, previous_interaction_id="int_1")

    with pytest.raises(ValueError, match="store=True"):
        adapter.build_payload(_request())


def test_previous_interaction_id_is_sent_when_state_is_stored():
    adapter = _adapter(store=True, previous_interaction_id="int_1")

    assert adapter.build_payload(_request())["previous_interaction_id"] == "int_1"


# --------------------------------------------------------------------------- streaming


def _text_stream():
    return [
        {"type": "interaction.created", "id": "int_abc"},
        {"type": "step.start", "index": 0, "step": {"type": "model_output"}},
        {"type": "step.delta", "index": 0, "delta": {"type": "text", "text": "Hello"}},
        {"type": "step.delta", "index": 0, "delta": {"type": "text", "text": " world"}},
        {"type": "step.stop", "index": 0},
        {
            "type": "interaction.completed",
            "interaction": {
                "id": "int_abc",
                "usage": {
                    "total_input_tokens": 10,
                    "total_output_tokens": 5,
                    "total_thought_tokens": 3,
                    "total_cached_tokens": 2,
                },
            },
        },
        {"type": "done"},
    ]


async def test_text_deltas_stream_through():
    adapter = _adapter(transport=_FakeTransport(events=_text_stream()))

    result = await collect(adapter.stream_response(_request()))

    assert result.text == "Hello world"
    assert result.response_id == "int_abc"
    assert not result.is_error


async def test_usage_is_mapped_from_the_completed_interaction():
    adapter = _adapter(transport=_FakeTransport(events=_text_stream()))

    result = await collect(adapter.stream_response(_request()))

    assert result.usage is not None
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5
    assert result.usage.cached_input_tokens == 2
    # Thought tokens are billed output the user never sees, so they are counted separately —
    # folding them into output_tokens makes a reasoning-heavy turn look far more productive.
    assert result.usage.reasoning_tokens == 3


async def test_fragmented_tool_arguments_are_parsed_once_at_the_stop():
    events = [
        {"type": "interaction.created", "id": "int_1"},
        {
            "type": "step.start",
            "index": 0,
            "step": {"type": "function_call", "id": "call_1", "name": "read", "arguments": {}},
        },
        {
            "type": "step.delta",
            "index": 0,
            "delta": {"type": "arguments_delta", "arguments": '{"pa'},
        },
        {
            "type": "step.delta",
            "index": 0,
            "delta": {"type": "arguments_delta", "arguments": 'th":'},
        },
        {
            "type": "step.delta",
            "index": 0,
            "delta": {"type": "arguments_delta", "arguments": '"a.txt"}'},
        },
        {"type": "step.stop", "index": 0},
        {"type": "done"},
    ]
    adapter = _adapter(transport=_FakeTransport(events=events))

    result = await collect(adapter.stream_response(_request()))

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "read"
    assert result.tool_calls[0].id == "call_1"
    assert result.tool_calls[0].arguments == {"path": "a.txt"}


async def test_parallel_tool_calls_keep_their_own_arguments():
    events = [
        {"type": "interaction.created", "id": "int_1"},
        {
            "type": "step.start",
            "index": 0,
            "step": {"type": "function_call", "id": "call_a", "name": "read"},
        },
        {
            "type": "step.start",
            "index": 1,
            "step": {"type": "function_call", "id": "call_b", "name": "write"},
        },
        {
            "type": "step.delta",
            "index": 1,
            "delta": {"type": "arguments_delta", "arguments": '{"b":2}'},
        },
        {
            "type": "step.delta",
            "index": 0,
            "delta": {"type": "arguments_delta", "arguments": '{"a":1}'},
        },
        {"type": "step.stop", "index": 0},
        {"type": "step.stop", "index": 1},
        {"type": "done"},
    ]
    adapter = _adapter(transport=_FakeTransport(events=events))

    result = await collect(adapter.stream_response(_request()))

    by_name = {call.name: call.arguments for call in result.tool_calls}
    assert by_name == {"read": {"a": 1}, "write": {"b": 2}}


async def test_invalid_tool_arguments_become_a_typed_error():
    events = [
        {"type": "interaction.created", "id": "int_1"},
        {
            "type": "step.start",
            "index": 0,
            "step": {"type": "function_call", "id": "call_1", "name": "read"},
        },
        {
            "type": "step.delta",
            "index": 0,
            "delta": {"type": "arguments_delta", "arguments": "{not json"},
        },
        {"type": "step.stop", "index": 0},
        {"type": "done"},
    ]
    adapter = _adapter(transport=_FakeTransport(events=events))

    result = await collect(adapter.stream_response(_request()))

    assert result.error_type == ErrorType.INVALID_TOOL_ARGUMENTS.value
    assert result.tool_calls == []


async def test_a_tool_call_missing_its_id_is_rejected_not_invented():
    events = [
        {"type": "interaction.created", "id": "int_1"},
        {"type": "step.start", "index": 0, "step": {"type": "function_call", "name": "read"}},
        {"type": "step.stop", "index": 0},
        {"type": "done"},
    ]
    adapter = _adapter(transport=_FakeTransport(events=events))

    result = await collect(adapter.stream_response(_request()))

    assert result.error_type == ErrorType.INVALID_TOOL_CALL.value


# --------------------------------------------------------------------------- reasoning


def _thought_stream():
    return [
        {"type": "interaction.created", "id": "int_1"},
        {"type": "step.start", "index": 0, "step": {"type": "thought"}},
        {
            "type": "step.delta",
            "index": 0,
            "delta": {"type": "thought_summary", "content": {"text": "considering options"}},
        },
        {"type": "step.stop", "index": 0},
        {"type": "step.start", "index": 1, "step": {"type": "model_output"}},
        {"type": "step.delta", "index": 1, "delta": {"type": "text", "text": "Answer"}},
        {"type": "step.stop", "index": 1},
        {"type": "done"},
    ]


async def test_thought_summaries_never_enter_the_event_stream():
    # Matches `openai_chat`, which drops reasoning so it never reaches an event, artifact or the
    # UI (spec §12). Emitting it here would make Gemini the one provider whose thoughts surface as
    # soon as any renderer starts reading that field.
    adapter = _adapter(transport=_FakeTransport(events=_thought_stream()))

    events_seen = [event async for event in adapter.stream_response(_request())]
    text = "".join(e.text or "" for e in events_seen)

    assert text == "Answer"
    assert all(e.reasoning is None for e in events_seen)
    assert "considering options" not in json.dumps([e.model_dump(mode="json") for e in events_seen])


async def test_thought_summaries_survive_for_replay():
    adapter = _adapter(transport=_FakeTransport(events=_thought_stream()))
    await collect(adapter.stream_response(_request()))

    envelope = adapter.build_continuation()
    thoughts = [s for s in envelope.native_steps if s.get("type") == "thought"]
    assert thoughts and thoughts[0]["thought_summary"] == "considering options"


async def test_a_non_streaming_thought_step_emits_nothing():
    body = {
        "id": "int_ns",
        "steps": [
            {"type": "thought", "thought_summary": "hidden"},
            {"type": "model_output", "text": "Result"},
        ],
    }
    adapter = _adapter(transport=_FakeTransport(post=body))

    events_seen = [event async for event in adapter.stream_response(_request(stream=False))]

    assert "hidden" not in json.dumps([e.model_dump(mode="json") for e in events_seen])


async def test_thought_signatures_are_captured_but_never_surfaced():
    events = [
        {"type": "interaction.created", "id": "int_1"},
        {"type": "step.start", "index": 0, "step": {"type": "thought"}},
        {
            "type": "step.delta",
            "index": 0,
            "delta": {"type": "thought_signature", "signature": "sig-xyz"},
        },
        {"type": "step.stop", "index": 0},
        {"type": "done"},
    ]
    adapter = _adapter(transport=_FakeTransport(events=events))

    seen = [event async for event in adapter.stream_response(_request())]

    assert all("sig-xyz" not in (e.text or "") for e in seen)
    assert all("sig-xyz" not in (e.reasoning or "") for e in seen)
    # ...but it survives for replay, which is the entire reason it is captured.
    envelope = adapter.build_continuation()
    assert envelope.native_steps[0]["thought_signature"] == "sig-xyz"


# --------------------------------------------------------------------------- continuation


async def test_stored_state_continues_by_interaction_id():
    adapter = _adapter(store=True, transport=_FakeTransport(events=_text_stream()))
    await collect(adapter.stream_response(_request()))

    envelope = adapter.build_continuation(model_id="gemini-3.6-flash")

    assert envelope.strategy is ContinuationStrategy.REMOTE_ID
    assert envelope.remote_interaction_id == "int_abc"
    assert envelope.protocol is Protocol.GEMINI_INTERACTIONS


async def test_local_only_state_continues_by_replaying_native_steps():
    adapter = _adapter(store=False, transport=_FakeTransport(events=_text_stream()))
    await collect(adapter.stream_response(_request()))

    envelope = adapter.build_continuation()

    assert envelope.strategy is ContinuationStrategy.NATIVE_STEPS_REPLAY
    assert envelope.native_steps
    assert envelope.remote_interaction_id is None


async def test_a_turn_with_no_steps_falls_back_to_normalized_history():
    adapter = _adapter(transport=_FakeTransport(events=[{"type": "done"}]))
    await collect(adapter.stream_response(_request()))

    assert adapter.build_continuation().strategy is ContinuationStrategy.NORMALIZED_HISTORY


async def test_an_unfinished_step_is_still_captured_for_replay():
    # A step that never got its stop is unfinished, not discarded — a replay still needs it.
    events = [
        {"type": "interaction.created", "id": "int_1"},
        {"type": "step.start", "index": 0, "step": {"type": "model_output"}},
        {"type": "step.delta", "index": 0, "delta": {"type": "text", "text": "partial"}},
    ]
    adapter = _adapter(transport=_FakeTransport(events=events))
    await collect(adapter.stream_response(_request()))

    envelope = adapter.build_continuation()
    assert envelope.native_steps[0]["text"] == "partial"


async def test_each_turn_starts_from_a_clean_step_list():
    transport = _FakeTransport(events=_text_stream())
    adapter = _adapter(transport=transport)
    await collect(adapter.stream_response(_request()))
    first = len(adapter.build_continuation().native_steps)

    adapter.transport = _FakeTransport(events=_text_stream())  # type: ignore[assignment]
    await collect(adapter.stream_response(_request()))

    assert len(adapter.build_continuation().native_steps) == first


# --------------------------------------------------------------------------- errors


async def test_a_stream_error_event_is_normalized():
    events = [
        {"type": "interaction.created", "id": "int_1"},
        {"type": "error", "error": {"message": "quota exceeded", "code": 429}},
    ]
    adapter = _adapter(transport=_FakeTransport(events=events))

    result = await collect(adapter.stream_response(_request()))

    assert result.error_type == ErrorType.PROVIDER_RATE_LIMITED.value
    assert "quota exceeded" in (result.error_message or "")


async def test_an_expired_interaction_is_not_reported_as_a_bad_request():
    # Telling the user to fix their prompt would send them after entirely the wrong thing.
    error = TransportError(
        ErrorType.INVALID_REQUEST, "previous_interaction_id not found", status=400
    )
    adapter = _adapter(transport=_FakeTransport(events=[error]))

    result = await collect(adapter.stream_response(_request()))

    assert result.error_type == ErrorType.REMOTE_SESSION_EXPIRED.value


async def test_an_unknown_model_is_reported_as_such():
    error = TransportError(ErrorType.MODEL_NOT_FOUND, "model gemini-x not found", status=404)
    adapter = _adapter(transport=_FakeTransport(events=[error]))

    result = await collect(adapter.stream_response(_request()))

    assert result.error_type == ErrorType.MODEL_NOT_FOUND.value


async def test_a_rejected_parameter_is_distinguished_from_a_bad_prompt():
    error = TransportError(ErrorType.INVALID_REQUEST, "unknown field: reasoning", status=400)
    adapter = _adapter(transport=_FakeTransport(events=[error]))

    result = await collect(adapter.stream_response(_request()))

    assert result.error_type == ErrorType.UNSUPPORTED_PARAMETER.value


async def test_an_auth_failure_passes_through_unchanged():
    error = TransportError(ErrorType.AUTHENTICATION_FAILED, "invalid key", status=401)
    adapter = _adapter(transport=_FakeTransport(events=[error]))

    result = await collect(adapter.stream_response(_request()))

    assert result.error_type == ErrorType.AUTHENTICATION_FAILED.value


# --------------------------------------------------------------------------- non-streaming


async def test_a_non_streaming_turn_maps_steps_to_events():
    body = {
        "id": "int_ns",
        "steps": [
            {"type": "thought", "thought_summary": "thinking"},
            {"type": "model_output", "text": "Result"},
        ],
        "usage": {"total_input_tokens": 4, "total_output_tokens": 2},
    }
    adapter = _adapter(transport=_FakeTransport(post=body))

    result = await collect(adapter.stream_response(_request(stream=False)))

    assert result.text == "Result"
    assert result.response_id == "int_ns"
    assert result.usage is not None and result.usage.input_tokens == 4


async def test_a_non_streaming_function_call_is_normalized():
    body = {
        "id": "int_ns",
        "steps": [
            {"type": "function_call", "id": "call_9", "name": "read", "arguments": {"path": "a"}}
        ],
    }
    adapter = _adapter(transport=_FakeTransport(post=body))

    result = await collect(adapter.stream_response(_request(stream=False)))

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].arguments == {"path": "a"}


# --------------------------------------------------------------------------- catalog


def _model(name: str, **extra) -> dict:
    return {"name": name, "displayName": name.split("/")[-1], "inputTokenLimit": 1_000_000, **extra}


async def test_models_are_listed_with_the_prefix_stripped():
    transport = _FakeTransport(gets=[{"models": [_model("models/gemini-3.6-flash")]}])
    adapter = _adapter(transport=transport)

    models = await adapter.list_models()

    assert [m.id for m in models] == ["gemini-3.6-flash"]
    assert models[0].context_window == 1_000_000


async def test_pagination_follows_the_next_page_token():
    transport = _FakeTransport(
        gets=[
            {"models": [_model("models/a")], "nextPageToken": "tok"},
            {"models": [_model("models/b")]},
        ]
    )
    adapter = _adapter(transport=transport)

    models = await adapter.list_models()

    assert [m.id for m in models] == ["a", "b"]
    assert "pageToken=tok" in transport.paths[1]


async def test_pagination_refuses_to_loop_forever():
    transport = _FakeTransport(
        gets=[{"models": [_model("models/a")], "nextPageToken": "same"} for _ in range(50)]
    )
    adapter = _adapter(transport=transport)

    with pytest.raises(ModelCatalogError, match="did not terminate"):
        await adapter.list_models()


async def test_malformed_entries_produce_an_honest_partial_catalog():
    transport = _FakeTransport(
        gets=[{"models": [_model("models/good"), {"no_name": 1}, "not-an-object"]}]
    )
    adapter = _adapter(transport=transport)

    with pytest.raises(ModelCatalogError) as exc:
        await adapter.list_models()

    # The usable entries survive on the exception rather than being discarded with the bad ones.
    assert [m.id for m in exc.value.models] == ["good"]
    assert "2 malformed" in str(exc.value)


async def test_a_missing_models_array_is_an_error_not_an_empty_catalog():
    # "This provider has no models" and "we could not read the list" look identical to a user.
    adapter = _adapter(transport=_FakeTransport(gets=[{"unexpected": True}]))

    with pytest.raises(ModelCatalogError, match="no models array"):
        await adapter.list_models()


async def test_an_empty_catalog_is_reported_as_empty_without_error():
    adapter = _adapter(transport=_FakeTransport(gets=[{"models": []}]))

    assert await adapter.list_models() == []


async def test_test_connection_reports_an_unreachable_endpoint():
    error = TransportError(ErrorType.CONNECTION_LOST, "network unreachable")
    adapter = _adapter(transport=_FakeTransport(gets=[error]))

    health = await adapter.test_connection()

    assert not health.ok
    assert "network unreachable" in health.detail


# --------------------------------------------------------------------------- redaction


async def test_no_fixture_path_writes_the_api_key_into_a_payload():
    transport = _FakeTransport(events=_text_stream())
    adapter = GeminiInteractionsAdapter(api_key="sk-secret-abcdef123456", transport=transport)

    await collect(adapter.stream_response(_request()))

    assert "sk-secret-abcdef123456" not in json.dumps(transport.posted)
