"""SSE streaming retry safety (spec §44).

A stream may be safely retried only before the first event. Once any event has been yielded, a
mid-stream disconnect must NOT replay the request (which would duplicate text / tool calls / file
changes); it must fail with a clear connection-lost error instead.
"""

from __future__ import annotations

import httpx
import pytest

from openagent.core.errors import ErrorType
from openagent.providers.transport import Transport, TransportError


class _Stream:
    """A fake httpx streaming response context manager."""

    def __init__(self, status: int, lines: list[str], *, raise_after: int | None = None) -> None:
        self.status_code = status
        self._lines = lines
        self._raise_after = raise_after

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self) -> bytes:
        return b'{"error": {"message": "boom"}}'

    async def aiter_lines(self):
        for i, line in enumerate(self._lines):
            if self._raise_after is not None and i >= self._raise_after:
                raise httpx.ReadError("connection dropped mid-stream")
            yield line

    @property
    def headers(self):
        return {}


class _Client:
    def __init__(self, streams: list[_Stream]) -> None:
        self._streams = streams
        self.calls = 0

    def stream(self, method, path, json=None):
        stream = self._streams[self.calls]
        self.calls += 1
        return stream


def _transport(client: _Client) -> Transport:
    t = Transport(base_url="https://api.test")
    t._client = client  # type: ignore[assignment]
    return t


async def test_disconnect_after_event_is_not_retried():
    # One tool-call event is delivered, then the stream drops. Must fail, not replay.
    lines = ['data: {"choices": [{"delta": {"tool_calls": [{"index": 0}]}}]}', "next"]
    client = _Client([_Stream(200, lines, raise_after=1)])
    transport = _transport(client)

    received = []
    with pytest.raises(TransportError) as exc:
        async for obj in transport.stream_sse("/chat", {}):
            received.append(obj)

    assert len(received) == 1  # the one event we got
    assert client.calls == 1  # the request was NOT replayed
    assert exc.value.error_type is ErrorType.CONNECTION_LOST


async def test_disconnect_before_event_is_retried():
    # First attempt drops before any event; second attempt succeeds.
    good = ['data: {"choices": [{"delta": {"content": "hi"}}]}', "data: [DONE]"]
    # First attempt raises on the very first line (before any event is yielded).
    client = _Client([_Stream(200, ["boom"], raise_after=0), _Stream(200, good)])
    transport = _transport(client)
    transport.backoff_base = 0.0  # no real sleeping

    received = [obj async for obj in transport.stream_sse("/chat", {})]
    assert client.calls == 2  # retried once
    assert received and received[0]["choices"][0]["delta"]["content"] == "hi"
