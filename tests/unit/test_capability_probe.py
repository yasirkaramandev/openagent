"""Capability probing only claims what it actually observed (item 9).

Drives the shared ``default_probe`` with a scripted fake adapter so each capability is checked with
the right request shape: streaming is never inferred from a non-stream reply, tool calling needs a
real tool call, system-prompt adherence needs the sentinel echoed, and a probe error never flips an
unverified capability to True.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from openagent.core.events import ModelEventType, NormalizedModelEvent, ToolCall
from openagent.providers.base import NormalizedModelRequest, default_probe

SENTINEL = "PROBE_OK_7F"


class FakeAdapter:
    def __init__(
        self,
        *,
        text: str = SENTINEL,
        stream_text: bool = True,
        tool: bool = True,
        text_error: bool = False,
        stream_error: bool = False,
    ) -> None:
        self.text = text
        self.stream_text = stream_text
        self.tool = tool
        self.text_error = text_error
        self.stream_error = stream_error
        self.requests: list[NormalizedModelRequest] = []

    async def stream_response(
        self, request: NormalizedModelRequest
    ) -> AsyncIterator[NormalizedModelEvent]:
        self.requests.append(request)
        if request.tools:  # tool probe
            if self.tool:
                yield NormalizedModelEvent(
                    type=ModelEventType.TOOL_CALL,
                    tool_call=ToolCall(id="c1", name="ping", arguments={"value": 1}),
                )
            yield NormalizedModelEvent(type=ModelEventType.DONE)
        elif request.stream:  # streaming probe
            if self.stream_error:
                yield NormalizedModelEvent(
                    type=ModelEventType.ERROR, error_type="provider_overloaded"
                )
                return
            if self.stream_text:
                yield NormalizedModelEvent(type=ModelEventType.TEXT_DELTA, text="streamed")
            yield NormalizedModelEvent(type=ModelEventType.DONE)
        else:  # text / system-prompt probe
            if self.text_error:
                yield NormalizedModelEvent(
                    type=ModelEventType.ERROR, error_type="authentication_failed"
                )
                return
            if self.text:
                yield NormalizedModelEvent(type=ModelEventType.TEXT_DELTA, text=self.text)
            yield NormalizedModelEvent(type=ModelEventType.DONE)


async def test_all_capabilities_verified():
    caps = await default_probe(FakeAdapter(), "m")
    assert caps.text is True
    assert caps.system_prompt is True
    assert caps.streaming is True
    assert caps.tool_calling is True


async def test_streaming_not_inferred_from_nonstream():
    # The model returns no text when actually streamed -> streaming stays unknown, never True.
    caps = await default_probe(FakeAdapter(stream_text=False), "m")
    assert caps.streaming is None


async def test_streaming_error_leaves_none_not_true():
    caps = await default_probe(FakeAdapter(stream_error=True), "m")
    assert caps.streaming is None


async def test_tool_calling_requires_actual_call():
    caps = await default_probe(FakeAdapter(tool=False), "m")
    assert caps.tool_calling is None


async def test_system_prompt_requires_sentinel_echo():
    caps = await default_probe(FakeAdapter(text="hello there"), "m")
    assert caps.text is True
    assert caps.system_prompt is None  # replied, but did not obey the system prompt


async def test_probe_error_asserts_nothing():
    caps = await default_probe(FakeAdapter(text_error=True), "m")
    assert caps.text is False
    assert caps.streaming is None
    assert caps.tool_calling is None
    assert caps.system_prompt is None


async def test_streaming_probe_actually_streams():
    fake = FakeAdapter()
    await default_probe(fake, "m")
    # A real stream=True request was issued (not just the non-stream text probe).
    assert any(r.stream for r in fake.requests)
    assert any(r.tools for r in fake.requests)
