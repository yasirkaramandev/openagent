"""NVIDIA Build contract tests (spec §20) — no real NVIDIA key required.

Everything here is driven by HTTP mocks/fixtures against the *official* contract (OpenAI Chat
Completions at https://integrate.api.nvidia.com/v1). The point is to prove the integration is honest:
a catalog entry is never assumed to be an agent-compatible chat model, a 202 is never read as an empty
success, the API key never appears in any error text, and raw ``reasoning_content`` never escapes.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from pytest_httpx import HTTPXMock

from openagent.core.models import Protocol
from openagent.providers.base import Message, NormalizedModelRequest, Role, collect
from openagent.providers.compat.profiles import get_compat
from openagent.providers.discovery import (
    PROBE_ASYNC_UNSUPPORTED,
    PROBE_INCOMPATIBLE,
    PROBE_NOT_FOUND,
    PROBE_PARTIAL,
    PROBE_RATE_LIMITED,
    PROBE_UNAUTHORIZED,
    PROBE_VERIFIED,
    filter_models,
    looks_non_chat,
    probe_agent_model,
    publishers,
)
from openagent.providers.factory import get_preset
from openagent.providers.openai_chat import OpenAIChatAdapter
from openagent.providers.transport import Transport

BASE = "https://integrate.api.nvidia.com/v1"
FAKE_KEY = "nvapi-THIS_IS_A_FAKE_TEST_KEY_123456"


def adapter(**kwargs) -> OpenAIChatAdapter:
    return OpenAIChatAdapter(
        base_url=BASE,
        api_key=FAKE_KEY,
        provider_type="nvidia-build",
        compat=get_compat("nvidia-build"),
        **kwargs,
    )


def req(stream: bool = False, tools=None) -> NormalizedModelRequest:
    return NormalizedModelRequest(
        model="nvidia/nemotron-test",
        messages=[Message(role=Role.USER, content="hi")],
        tools=tools or [],
        stream=stream,
    )


def sse(chunks: list[dict]) -> bytes:
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


# --------------------------------------------------------------------------- §10 preset / §11 compat


def test_nvidia_preset_matches_the_official_contract():
    preset = get_preset("nvidia-build")
    assert preset is not None
    assert preset.label == "NVIDIA Build (Hosted NIM APIs)"
    assert preset.protocol is Protocol.OPENAI_CHAT
    assert preset.openai_base_url == "https://integrate.api.nvidia.com/v1"
    assert preset.needs_key is True
    assert preset.default_env_var == "NVIDIA_API_KEY"
    assert preset.catalog_url == "https://build.nvidia.com/"
    assert preset.model_id_hint == "publisher/model"
    # The catalog mixes model types, so a listing is never a capability claim (§14.3).
    assert preset.catalog_is_mixed is True


async def test_stream_options_is_not_sent_by_default(httpx_mock: HTTPXMock):
    """``stream_options`` is undocumented in NVIDIA's official examples — do not send it (§11)."""

    httpx_mock.add_response(
        content=sse([{"id": "c1", "choices": [{"delta": {"content": "ok"}}]}]),
        headers={"content-type": "text/event-stream"},
    )
    await collect(adapter().stream_response(req(stream=True)))
    sent = json.loads(httpx_mock.get_requests()[0].content)
    assert "stream_options" not in sent
    # Only the common OpenAI-compatible fields are sent for the base integration (§11).
    assert set(sent) <= {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "temperature",
        "max_tokens",
        "stream",
    }


# --------------------------------------------------------------------------- §20.1 catalog


_CATALOG = {
    "object": "list",
    "data": [
        {"id": "nvidia/nemotron-test", "owned_by": "nvidia"},
        {"id": "nvidia/embed-test", "owned_by": "nvidia"},
        {"id": "meta/vision-test", "owned_by": "meta"},
        {"id": "deepseek-ai/chat-test", "owned_by": "deepseek-ai"},
    ],
}


async def test_catalog_lists_every_entry_and_preserves_owned_by(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json=_CATALOG)
    models = await adapter().list_models()
    assert [m.id for m in models] == [
        "nvidia/nemotron-test",
        "nvidia/embed-test",
        "meta/vision-test",
        "deepseek-ai/chat-test",
    ]
    assert [m.owned_by for m in models] == ["nvidia", "nvidia", "meta", "deepseek-ai"]


async def test_no_catalog_entry_is_automatically_agent_compatible(httpx_mock: HTTPXMock):
    """Listing a model proves nothing about its capabilities (§14.3) — a probe is the only authority."""

    httpx_mock.add_response(json=_CATALOG)
    models = await adapter().list_models()
    for model in models:
        caps = getattr(model, "capabilities", None)
        assert caps is None, "a catalog entry must not carry assumed capabilities"


async def test_catalog_search_and_owner_filter(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json=_CATALOG)
    models = await adapter().list_models()

    assert [m.id for m in filter_models(models, search="nemotron")] == ["nvidia/nemotron-test"]
    assert [m.id for m in filter_models(models, owner="meta")] == ["meta/vision-test"]
    assert [m.id for m in filter_models(models, owner="nvidia", search="embed")] == [
        "nvidia/embed-test"
    ]
    assert filter_models(models, search="NEMOTRON")  # case-insensitive
    assert publishers(models) == ["deepseek-ai", "meta", "nvidia"]


def test_non_chat_hints_are_warnings_not_verdicts():
    # These only *hint* — they must never be used to accept or reject a model (§14.3).
    assert looks_non_chat("nvidia/embed-test")
    assert looks_non_chat("meta/vision-test")
    assert not looks_non_chat("nvidia/nemotron-test")


# --------------------------------------------------------------------------- §20.2 auth errors


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_errors_never_leak_the_key(httpx_mock: HTTPXMock, status: int):
    httpx_mock.add_response(
        status_code=status, json={"error": {"message": "invalid api key supplied"}}
    )
    result = await collect(adapter().stream_response(req()))
    assert result.is_error
    assert result.error_type in ("authentication_failed", "permission_denied")
    assert FAKE_KEY not in (result.error_message or "")
    assert "nvapi-" not in (result.error_message or "")


async def test_probe_reports_unauthorized_for_a_rejected_key(httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=401, json={"error": {"message": "bad key"}})
    probe = await probe_agent_model(adapter(), "nvidia/nemotron-test")
    assert probe.category == PROBE_UNAUTHORIZED
    assert probe.agent_compatible is False
    assert probe.capabilities.text is False
    assert FAKE_KEY not in probe.detail


# --------------------------------------------------------------------------- §20.3 model compatibility


async def test_normal_text_completion(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json={
            "id": "c1",
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        }
    )
    result = await collect(adapter().stream_response(req()))
    assert result.text == "hello"
    assert result.usage.input_tokens == 4


async def test_sse_streaming(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        content=sse(
            [
                {"id": "c1", "choices": [{"delta": {"content": "Hel"}}]},
                {"id": "c1", "choices": [{"delta": {"content": "lo"}}]},
            ]
        ),
        headers={"content-type": "text/event-stream"},
    )
    result = await collect(adapter().stream_response(req(stream=True)))
    assert result.text == "Hello"


async def test_tool_call_response(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json={
            "id": "c1",
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "ping", "arguments": json.dumps({"value": 1})},
                            }
                        ],
                    }
                }
            ],
        }
    )
    result = await collect(adapter().stream_response(req(tools=[{"name": "ping"}])))
    assert result.tool_calls[0].name == "ping"
    assert result.tool_calls[0].arguments == {"value": 1}


async def test_probe_verified_when_all_three_capabilities_observed(httpx_mock: HTTPXMock):
    # 1) text (non-stream)  2) streaming  3) tool call
    httpx_mock.add_response(json={"id": "c1", "choices": [{"message": {"content": "ok"}}]})
    httpx_mock.add_response(
        content=sse([{"id": "c1", "choices": [{"delta": {"content": "ok"}}]}]),
        headers={"content-type": "text/event-stream"},
    )
    httpx_mock.add_response(
        json={
            "id": "c1",
            "choices": [
                {
                    "message": {
                        "content": None,
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
    probe = await probe_agent_model(adapter(), "nvidia/nemotron-test")
    assert probe.category == PROBE_VERIFIED
    assert probe.agent_compatible is True
    assert probe.capabilities.text and probe.capabilities.streaming
    assert probe.capabilities.tool_calling is True
    assert probe.message() == "Verified Agent Compatible"


async def test_probe_partial_when_tool_calling_is_unsupported(httpx_mock: HTTPXMock):
    """Text works but no tool call comes back → partial, and NEVER agent_compatible (§15.3)."""

    httpx_mock.add_response(json={"id": "c1", "choices": [{"message": {"content": "ok"}}]})
    httpx_mock.add_response(
        content=sse([{"id": "c1", "choices": [{"delta": {"content": "ok"}}]}]),
        headers={"content-type": "text/event-stream"},
    )
    httpx_mock.add_response(json={"id": "c1", "choices": [{"message": {"content": "I cannot"}}]})
    probe = await probe_agent_model(adapter(), "nvidia/nemotron-test")
    assert probe.category == PROBE_PARTIAL
    assert probe.agent_compatible is False
    assert probe.capabilities.tool_calling is None  # unproven, never False-as-fact or True
    assert "tool calling was not verified" in probe.message()


async def test_chat_endpoint_422_is_incompatible(httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=422, json={"error": {"message": "unsupported input"}})
    probe = await probe_agent_model(adapter(), "nvidia/embed-test")
    assert probe.category == PROBE_INCOMPATIBLE
    assert probe.agent_compatible is False
    assert "not compatible" in probe.message()


async def test_model_404_is_not_found(httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=404, json={"error": {"message": "model not found"}})
    probe = await probe_agent_model(adapter(), "nvidia/gone")
    assert probe.category == PROBE_NOT_FOUND
    assert "no longer available" in probe.message()


async def test_rate_limit_429(httpx_mock: HTTPXMock):
    # max_retries=1 → one initial attempt plus one retry, then the error surfaces.
    for _ in range(2):
        httpx_mock.add_response(status_code=429, json={"error": {"message": "slow down"}})
    transport = Transport(base_url=BASE, headers={}, max_retries=1, backoff_base=0.0)
    probe = await probe_agent_model(adapter(transport=transport), "nvidia/nemotron-test")
    assert probe.category == PROBE_RATE_LIMITED
    assert "Rate limit" in probe.message()


async def test_async_202_is_rejected_not_treated_as_empty_success(httpx_mock: HTTPXMock):
    """A 202 + request id must be an explicit failure, never an empty completion (§15.5)."""

    httpx_mock.add_response(status_code=202, json={"request_id": "req-123", "status": "pending"})
    result = await collect(adapter().stream_response(req()))
    assert result.is_error
    assert result.error_type == "async_unsupported"

    httpx_mock.reset()
    httpx_mock.add_response(status_code=202, json={"request_id": "req-123"})
    probe = await probe_agent_model(adapter(), "nvidia/async-model")
    assert probe.category == PROBE_ASYNC_UNSUPPORTED
    assert probe.agent_compatible is False
    assert "not supported" in probe.message()


async def test_malformed_sse_is_safe(httpx_mock: HTTPXMock):
    body = b'data: {not valid json\n\ndata: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
    httpx_mock.add_response(content=body, headers={"content-type": "text/event-stream"})
    result = await collect(adapter().stream_response(req(stream=True)))
    assert result.text == "ok"  # the unparseable frame is skipped, not fatal


async def test_connection_timeout_is_classified(httpx_mock: HTTPXMock):
    import httpx

    httpx_mock.add_exception(httpx.ConnectTimeout("timed out"))
    transport = Transport(base_url=BASE, headers={}, max_retries=0, backoff_base=0.0)
    result = await collect(adapter(transport=transport).stream_response(req()))
    assert result.is_error
    assert result.error_type == "timeout"


async def test_cancellation_during_a_stalled_stream(httpx_mock: HTTPXMock):
    """A model that accepts the request then goes silent must still be cancellable (§20.3)."""

    async def _stall(request):  # noqa: ANN001 - pytest_httpx callback
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    httpx_mock.add_callback(_stall)
    stream = adapter().stream_response(req(stream=True))
    task = asyncio.create_task(collect(stream))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --------------------------------------------------------------------------- §20.4 raw reasoning


async def test_raw_reasoning_content_is_never_surfaced(httpx_mock: HTTPXMock):
    """``reasoning_content`` is raw chain-of-thought: it must never reach text/events (§12).

    Only the final ``content`` is a user-visible message.
    """

    httpx_mock.add_response(
        content=sse(
            [
                {
                    "choices": [
                        {
                            "delta": {
                                "reasoning_content": "PRIVATE INTERNAL REASONING",
                                "content": None,
                            }
                        }
                    ]
                },
                {"choices": [{"delta": {"content": "Safe final answer"}}]},
            ]
        ),
        headers={"content-type": "text/event-stream"},
    )
    result = await collect(adapter().stream_response(req(stream=True)))
    assert result.text == "Safe final answer"
    assert "PRIVATE INTERNAL REASONING" not in result.text


async def test_non_streaming_reasoning_field_is_ignored(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json={
            "id": "c1",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "PRIVATE INTERNAL REASONING",
                        "content": "Safe final answer",
                    }
                }
            ],
        }
    )
    result = await collect(adapter().stream_response(req()))
    assert result.text == "Safe final answer"
    assert "PRIVATE" not in result.text


async def test_numeric_reasoning_tokens_are_normalized_without_any_text(httpx_mock: HTTPXMock):
    """Only the reasoning token *count* may be kept — never reasoning text (§12)."""

    httpx_mock.add_response(
        json={
            "id": "c1",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": 4,
                "completion_tokens": 9,
                "completion_tokens_details": {"reasoning_tokens": 7},
            },
        }
    )
    result = await collect(adapter().stream_response(req()))
    assert result.usage.reasoning_tokens == 7
    assert result.text == "ok"
