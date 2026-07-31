"""Server-sent event framing, to the spec rather than to the common case (spec §8).

The parser this replaces treated every ``data:`` line as one complete JSON document. That is right
for the traffic every provider actually sends today and wrong about the format they are all
claiming to speak, which means the failures it produces are rare, provider-specific and awful to
diagnose: an event split across two ``data:`` lines silently became two half-documents, and a
stream truncated mid-event dispatched the fragment as though the server had finished it.

The rules exercised here are WHATWG's: fields are ``event``/``data``/``id``/``retry``, multiple
``data`` lines within one event join with a newline, a blank line is what dispatches an event, and
an event still buffered at EOF never happened.
"""

from __future__ import annotations

from openagent.providers.sse import SseEvent, iter_sse_events, parse_sse_bytes


def events(raw: str | bytes) -> list[SseEvent]:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    return list(parse_sse_bytes([data]))


# --------------------------------------------------------------------------- data joining


def test_sse_parser_joins_multiple_data_lines() -> None:
    """One event, split over two ``data:`` lines, is one document — joined with a newline.

    The old reader produced two events here, each an unparseable half of a JSON object.
    """

    [event] = events('data: {"a":\ndata: 1}\n\n')
    assert event.data == '{"a":\n1}'


def test_a_data_line_with_no_space_after_the_colon_keeps_its_value() -> None:
    """Exactly one leading space is stripped, and only if present."""

    assert events("data:no-space\n\n")[0].data == "no-space"
    assert events("data:  two-spaces\n\n")[0].data == " two-spaces"


def test_a_bare_data_field_contributes_an_empty_line() -> None:
    [event] = events("data: first\ndata:\ndata: third\n\n")
    assert event.data == "first\n\nthird"


# --------------------------------------------------------------------------- line terminators


def test_sse_parser_supports_cr_lf_and_crlf() -> None:
    """All three terminators, including a lone CR, which httpx's line splitter alone gets wrong."""

    assert events("data: lf\n\n")[0].data == "lf"
    assert events("data: crlf\r\n\r\n")[0].data == "crlf"
    assert events("data: cr\r\r")[0].data == "cr"


def test_a_crlf_split_across_two_chunks_is_one_terminator() -> None:
    """The CR ends one read and the LF starts the next; treating that as two lines dispatches early."""

    parsed = list(parse_sse_bytes([b"data: split\r", b"\n\r\n"]))
    assert [e.data for e in parsed] == ["split"]


def test_a_utf8_bom_is_stripped_once() -> None:
    assert events(b"\xef\xbb\xbfdata: bom\n\n")[0].data == "bom"


def test_a_multibyte_character_split_across_chunks_survives() -> None:
    """Incremental decoding, not decode-per-chunk: 'ö' is two bytes and they can arrive apart."""

    parsed = list(parse_sse_bytes([b"data: caf\xc3", b"\xa9\n\n"]))
    assert [e.data for e in parsed] == ["café"]


# --------------------------------------------------------------------------- fields


def test_event_id_and_retry_are_parsed() -> None:
    [event] = events("event: step.delta\nid: 42\nretry: 3000\ndata: payload\n\n")
    assert event.event == "step.delta"
    assert event.event_id == "42"
    assert event.retry == 3000
    assert event.data == "payload"


def test_a_non_numeric_retry_is_ignored_rather_than_fatal() -> None:
    [event] = events("retry: soon\ndata: x\n\n")
    assert event.retry is None


def test_comments_and_unknown_fields_are_ignored() -> None:
    [event] = events(": keep-alive\nfoo: bar\ndata: real\n\n")
    assert event.data == "real"


def test_a_comment_only_block_dispatches_nothing() -> None:
    assert events(": ping\n\n: ping\n\n") == []


# --------------------------------------------------------------------------- dispatch rules


def test_sse_incomplete_final_event_is_discarded() -> None:
    """No blank line means the server never finished the event. Acting on it is acting on a guess."""

    assert events("data: complete\n\ndata: truncated") == [SseEvent(data="complete")]


def test_an_event_with_no_data_field_is_not_dispatched() -> None:
    """``id``-only blocks are legal reconnection bookkeeping, not events."""

    parsed = events("id: 7\n\ndata: real\n\n")
    assert [e.data for e in parsed] == ["real"]


def test_the_last_event_id_persists_across_events() -> None:
    """WHATWG keeps the id as *stream* state: it is what a reconnect resumes from, so it carries
    forward until the server sends another one. Resetting it per event would lose the resume point.
    """

    parsed = events("id: 7\ndata: a\n\ndata: b\n\n")
    assert [(e.event_id, e.data) for e in parsed] == [("7", "a"), ("7", "b")]


def test_state_does_not_leak_between_events() -> None:
    parsed = events("event: first\ndata: one\n\ndata: two\n\n")
    assert [(e.event, e.data) for e in parsed] == [("first", "one"), (None, "two")]


# --------------------------------------------------------------------------- the sentinel


def test_the_parser_does_not_know_about_done() -> None:
    """``[DONE]`` is an OpenAI convention, not SSE. Hard-coding it here breaks every other wire."""

    assert [e.data for e in events("data: [DONE]\n\n")] == ["[DONE]"]


async def test_iter_sse_events_streams_an_async_source() -> None:
    async def source():
        yield b"data: a\n\nda"
        yield b"ta: b\n\n"

    assert [e.data async for e in iter_sse_events(source())] == ["a", "b"]
