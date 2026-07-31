"""Opt-in live smoke against the real Gemini Interactions API (spec §10.7, §28).

Runs only with a real key in ``OPENAGENT_LIVE_GEMINI_API_KEY``. Every request here is deliberately
tiny — a handful of output tokens, one no-op tool, no retries beyond the transport's own — because
this suite exists to prove the wire format is what the documentation says, not to exercise the
model. Spend is the reason it is opt-in.

The two things these tests exist to catch are the two the offline contract tests structurally
cannot: whether the streamed response really is SSE-framed, and whether the documented step/delta
event names are the ones the endpoint actually emits.

A skip here is ``BLOCKED_BY_CREDENTIAL``, which is not a pass. Nothing in this file may be reported
as live verification unless it ran.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from openagent.core.events import ModelEventType
from openagent.providers.base import Message, NormalizedModelRequest, Role, collect
from openagent.providers.continuation import ContinuationStrategy
from openagent.providers.gemini_interactions import GeminiInteractionsAdapter

_ENV_KEY = "OPENAGENT_LIVE_GEMINI_API_KEY"
_MODEL_ENV = "OPENAGENT_LIVE_GEMINI_MODEL"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(
        not os.environ.get(_ENV_KEY),
        reason=f"set {_ENV_KEY} to run the Gemini live smoke (BLOCKED_BY_CREDENTIAL, not a pass)",
    ),
]

#: Per-request ceiling. A live smoke that can hang is a live smoke that gets disabled.
_TIMEOUT = 60.0


def _adapter(**kwargs) -> GeminiInteractionsAdapter:
    return GeminiInteractionsAdapter(api_key=os.environ[_ENV_KEY], **kwargs)


async def _model_id(adapter: GeminiInteractionsAdapter) -> str:
    """The model to exercise: the operator's choice, or the first the catalog offers.

    Never a hardcoded model name. Model ids move, and a suite that pins one starts failing for a
    reason that has nothing to do with the adapter (spec: do not guess model names).
    """

    override = os.environ.get(_MODEL_ENV)
    if override:
        return override
    models = await asyncio.wait_for(adapter.list_models(), timeout=_TIMEOUT)
    if not models:
        pytest.skip("Gemini catalog returned no models; nothing to exercise")
    return models[0].id


def _tiny(model: str, **kwargs) -> NormalizedModelRequest:
    defaults = {
        "model": model,
        "messages": [Message(role=Role.USER, content="Reply with the single word: ok")],
        "max_tokens": 16,
    }
    defaults.update(kwargs)
    return NormalizedModelRequest(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- catalog


async def test_list_models_returns_a_usable_catalog():
    adapter = _adapter()

    models = await asyncio.wait_for(adapter.list_models(), timeout=_TIMEOUT)

    assert models, "Gemini returned an empty catalog"
    assert all(model.id and "/" not in model.id for model in models)
    assert len({model.id for model in models}) == len(models)


async def test_connection_check_succeeds_with_a_real_key():
    health = await asyncio.wait_for(_adapter().test_connection(), timeout=_TIMEOUT)

    assert health.ok, health.detail


# --------------------------------------------------------------------------- inference


async def test_a_small_non_streaming_turn_returns_text():
    adapter = _adapter()
    model = await _model_id(adapter)

    result = await asyncio.wait_for(
        collect(adapter.stream_response(_tiny(model, stream=False))), timeout=_TIMEOUT
    )

    assert not result.is_error, result.error_message
    assert result.text.strip()
    assert result.response_id


async def test_a_small_streaming_turn_arrives_as_deltas():
    """The test the offline fixtures cannot do: is the stream really SSE-framed?"""

    adapter = _adapter()
    model = await _model_id(adapter)

    async def _drain() -> list:
        return [
            event
            async for event in adapter.stream_response(_tiny(model, stream=True))
            if event.type is ModelEventType.TEXT_DELTA and event.text
        ]

    # Wrapped like every other request here: the suite must not be able to hang, and the dev extras
    # carry no pytest timeout plugin to catch it if it did.
    deltas = await asyncio.wait_for(_drain(), timeout=_TIMEOUT)

    assert deltas, "no text deltas arrived; the stream framing is not what the adapter assumes"
    assert "".join(d.text or "" for d in deltas).strip()


async def test_usage_is_reported():
    adapter = _adapter()
    model = await _model_id(adapter)

    result = await asyncio.wait_for(
        collect(adapter.stream_response(_tiny(model, stream=False))), timeout=_TIMEOUT
    )

    assert result.usage is not None
    assert result.usage.input_tokens > 0


# --------------------------------------------------------------------------- tools


_NOOP_TOOL = {
    "name": "ping",
    "description": "A health probe that takes no meaningful action.",
    "parameters": {
        "type": "object",
        "properties": {"value": {"type": "integer", "description": "any integer"}},
        "required": ["value"],
    },
}


async def test_a_single_no_op_tool_can_be_called():
    adapter = _adapter()
    model = await _model_id(adapter)
    request = _tiny(
        model,
        messages=[Message(role=Role.USER, content="Call the ping tool with value 1.")],
        tools=[_NOOP_TOOL],
        max_tokens=64,
        stream=False,
    )

    result = await asyncio.wait_for(collect(adapter.stream_response(request)), timeout=_TIMEOUT)

    assert not result.is_error, result.error_message
    # A model declining to call the tool does not disprove tool support, so this is a skip rather
    # than a failure — asserting otherwise would make the suite flaky on a model's discretion.
    if not result.tool_calls:
        pytest.skip("model chose not to call the tool; tool support is unproven, not disproven")
    call = result.tool_calls[0]
    assert call.id and call.name == "ping"
    assert isinstance(call.arguments, dict)


# --------------------------------------------------------------------------- continuation


async def test_a_stateful_follow_up_uses_previous_interaction_id():
    """Requires store=true — the provider can only continue what it was asked to keep."""

    adapter = _adapter(store=True)
    model = await _model_id(adapter)

    first = await asyncio.wait_for(
        collect(adapter.stream_response(_tiny(model, stream=False))), timeout=_TIMEOUT
    )
    assert not first.is_error, first.error_message
    envelope = adapter.build_continuation(model_id=model)
    assert envelope.strategy is ContinuationStrategy.REMOTE_ID

    follow_up = _adapter(store=True, previous_interaction_id=envelope.remote_interaction_id)
    second = await asyncio.wait_for(
        collect(
            follow_up.stream_response(
                _tiny(
                    model,
                    messages=[Message(role=Role.USER, content="And again, one word.")],
                    stream=False,
                )
            )
        ),
        timeout=_TIMEOUT,
    )

    assert not second.is_error, second.error_message
    assert second.text.strip()


async def test_a_stateless_follow_up_replays_native_steps():
    """The local-only path: no server-side state, continuation carried by native steps."""

    adapter = _adapter(store=False)
    model = await _model_id(adapter)

    first = await asyncio.wait_for(
        collect(adapter.stream_response(_tiny(model, stream=False))), timeout=_TIMEOUT
    )
    assert not first.is_error, first.error_message

    envelope = adapter.build_continuation(model_id=model)
    assert envelope.strategy in {
        ContinuationStrategy.NATIVE_STEPS_REPLAY,
        ContinuationStrategy.NORMALIZED_HISTORY,
    }

    replayed = _adapter(store=False)
    second = await asyncio.wait_for(
        collect(
            replayed.stream_response(
                _tiny(
                    model,
                    messages=[
                        Message(role=Role.USER, content="Reply with the single word: ok"),
                        Message(
                            role=Role.ASSISTANT,
                            content=first.text,
                            raw_blocks=list(envelope.native_steps) or None,
                        ),
                        Message(role=Role.USER, content="And again, one word."),
                    ],
                    stream=False,
                )
            )
        ),
        timeout=_TIMEOUT,
    )

    assert not second.is_error, second.error_message


# --------------------------------------------------------------------------- cancellation


async def test_a_stream_can_be_cancelled_mid_turn():
    adapter = _adapter()
    model = await _model_id(adapter)
    request = _tiny(
        model,
        messages=[Message(role=Role.USER, content="Count slowly from 1 to 200.")],
        max_tokens=512,
        stream=True,
    )

    stream = adapter.stream_response(request)
    async for _event in stream:
        break
    await stream.aclose()

    # Closing must not leave the transport unusable for the next request.
    health = await asyncio.wait_for(adapter.test_connection(), timeout=_TIMEOUT)
    assert health.ok, health.detail


# --------------------------------------------------------------------------- errors


async def test_an_unknown_model_is_reported_as_model_not_found():
    adapter = _adapter()
    request = _tiny("definitely-not-a-real-model-xyz", stream=False)

    result = await asyncio.wait_for(collect(adapter.stream_response(request)), timeout=_TIMEOUT)

    assert result.is_error
    assert result.error_type in {"model_not_found", "invalid_request", "permission_denied"}


async def test_an_invalid_key_is_reported_as_authentication_failed():
    adapter = GeminiInteractionsAdapter(api_key="not-a-real-key")
    request = _tiny("gemini-does-not-matter-here", stream=False)

    result = await asyncio.wait_for(collect(adapter.stream_response(request)), timeout=_TIMEOUT)

    assert result.is_error
    assert result.error_type in {"authentication_failed", "permission_denied", "invalid_request"}
    # Whatever the provider says, the key must not come back in the message.
    assert "not-a-real-key" not in (result.error_message or "")
