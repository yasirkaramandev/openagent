"""Server-sent event framing (spec §8).

This is the *format*, and nothing above it. It does not decode JSON, does not know what ``[DONE]``
means, and does not know which provider it is talking to — those belong to the wire, which is the
layer that knows what its events contain. Keeping them out is what lets one parser serve nine
providers instead of becoming the place every provider's quirk accumulates.

What it replaces read one line at a time and treated each ``data:`` line as a whole JSON document.
That is correct for the traffic providers actually send and incorrect about the format they all
claim to speak, so the bugs it produced were rare and provider-shaped: an event legitimately split
across two ``data:`` lines became two unparseable halves, and a stream cut mid-event dispatched the
fragment as though the server had finished it.

The parser is a byte-level state machine because the two hard cases are both sub-line:

* a CRLF can be split across two network reads, and treating the halves as two terminators
  dispatches an event early;
* a multi-byte character can be split across two reads, and decoding each read independently
  corrupts it.

An incremental decoder and a held-over CR flag are what make those non-events.
"""

from __future__ import annotations

import codecs
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass

#: Ceiling on a single event's accumulated data. A server that never sends a blank line would
#: otherwise buffer without bound; the stream is the attacker-controlled input here.
MAX_EVENT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class SseEvent:
    """One dispatched event.

    ``data`` is the joined payload with no trailing newline, exactly as WHATWG specifies. ``event``
    is ``None`` when the server sent no ``event:`` field, which callers read as "the default event
    type" rather than as an absent one.
    """

    data: str
    event: str | None = None
    event_id: str | None = None
    retry: int | None = None


class SseParser:
    """Feed it bytes, take events out. One instance per stream."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""
        #: True when the previous chunk ended on a CR, so a leading LF now is that CR's other half
        #: rather than an empty line that would dispatch the event early.
        self._pending_cr = False
        self._checked_bom = False
        self._data: list[str] = []
        self._data_bytes = 0
        self._event: str | None = None
        self._event_id: str | None = None
        self._retry: int | None = None
        self._saw_field = False

    # ------------------------------------------------------------------ feeding

    def feed(self, chunk: bytes) -> Iterator[SseEvent]:
        text = self._decoder.decode(chunk)
        if not self._checked_bom and text:
            # Strip a leading BOM once. It is legal for a server to send one and it is not part of
            # the first field name, which would otherwise be "﻿data" and silently ignored.
            self._checked_bom = True
            text = text.lstrip("﻿")
        if not text:
            return
        yield from self._consume(text)

    def close(self) -> Iterator[SseEvent]:
        """Flush the decoder at EOF.

        Deliberately yields nothing of its own: an event still buffered here never received the
        blank line that dispatches it, so the server did not finish sending it, and dispatching it
        anyway is how a truncated stream becomes a tool call the model never completed.
        """

        tail = self._decoder.decode(b"", True)
        if tail:
            yield from self._consume(tail)
        # Whatever remains in _data is discarded with the parser.
        return

    # ------------------------------------------------------------------ line splitting

    def _consume(self, text: str) -> Iterator[SseEvent]:
        self._buffer += text
        while True:
            line, rest = self._split_line(self._buffer)
            if line is None:
                break
            self._buffer = rest
            event = self._handle_line(line)
            if event is not None:
                yield event

    def _split_line(self, buffer: str) -> tuple[str | None, str]:
        """Pull one complete line, honouring LF, CRLF and a lone CR.

        Returns ``(None, buffer)`` when no terminator has arrived yet — a bare CR at the very end is
        not yet a line, because the next byte may be the LF that pairs with it.
        """

        start = 0
        if self._pending_cr:
            self._pending_cr = False
            if buffer.startswith("\n"):
                start = 1

        index = _find_terminator(buffer, start)
        if index is None:
            return None, buffer[start:] if start else buffer

        line = buffer[start:index]
        char = buffer[index]
        if char == "\r":
            if index + 1 >= len(buffer):
                # Might be a CRLF whose LF has not arrived. Remember, and swallow a leading LF next.
                self._pending_cr = True
                return line, ""
            skip = 2 if buffer[index + 1] == "\n" else 1
            return line, buffer[index + skip :]
        return line, buffer[index + 1 :]

    # ------------------------------------------------------------------ field handling

    def _handle_line(self, line: str) -> SseEvent | None:
        if not line:
            return self._dispatch()
        if line.startswith(":"):
            return None  # comment / keep-alive
        name, _, value = line.partition(":")
        # Exactly one leading space is removed, so "data:  x" really does carry a leading space.
        if value.startswith(" "):
            value = value[1:]
        self._set_field(name, value)
        return None

    def _set_field(self, name: str, value: str) -> None:
        if name == "data":
            size = len(value.encode("utf-8", "ignore")) + 1
            if self._data_bytes + size > MAX_EVENT_BYTES:
                return
            self._data.append(value)
            self._data_bytes += size
            self._saw_field = True
        elif name == "event":
            self._event = value
            self._saw_field = True
        elif name == "id":
            # WHATWG: an id containing NUL is ignored outright.
            if "\x00" not in value:
                self._event_id = value
                self._saw_field = True
        elif name == "retry":
            if value.isdigit():
                self._retry = int(value)
                self._saw_field = True
        # Any other field name is ignored, per spec — including a BOM-mangled one.

    def _dispatch(self) -> SseEvent | None:
        """A blank line ends the event. Only a block that carried data becomes one."""

        had_data = bool(self._data)
        event = SseEvent(
            data="\n".join(self._data),
            event=self._event,
            event_id=self._event_id,
            retry=self._retry,
        )
        self._data = []
        self._data_bytes = 0
        self._event = None
        self._retry = None
        self._saw_field = False
        # NB: _event_id deliberately survives, because the last-seen id is stream state used for
        # reconnection, not per-event state. It is reported on every subsequent event.
        return event if had_data else None


def _find_terminator(buffer: str, start: int) -> int | None:
    lf = buffer.find("\n", start)
    cr = buffer.find("\r", start)
    if lf == -1 and cr == -1:
        return None
    if lf == -1:
        return cr
    if cr == -1:
        return lf
    return min(lf, cr)


def parse_sse_bytes(chunks: Iterable[bytes]) -> Iterator[SseEvent]:
    """Parse a synchronous sequence of byte chunks. Chunk boundaries are irrelevant to the result."""

    parser = SseParser()
    for chunk in chunks:
        yield from parser.feed(chunk)
    yield from parser.close()


async def iter_sse_events(chunks: AsyncIterator[bytes]) -> AsyncIterator[SseEvent]:
    """Parse an async byte stream — the shape httpx's ``aiter_bytes()`` produces."""

    parser = SseParser()
    async for chunk in chunks:
        for event in parser.feed(chunk):
            yield event
    for event in parser.close():
        yield event
