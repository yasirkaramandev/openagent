"""The shared streaming accumulator (spec §11).

These are the fragment shapes that break per-adapter accumulators: JSON split mid-key, ids that
appear once, indexes that don't, duplicate ids, parallel calls interleaved, and a stream that stops
in the middle of an argument. Each one is a bug someone would otherwise write six times.
"""

from __future__ import annotations

import pytest

from openagent.providers.streaming import (
    MAX_TOOL_ARGUMENT_BYTES,
    FinishReason,
    StreamingTurnAssembler,
    Usage,
)


def test_text_deltas_concatenate_in_order() -> None:
    a = StreamingTurnAssembler()
    for piece in ("Hel", "lo, ", "world"):
        a.append_text(piece)
    assert a.build().text == "Hello, world"


def test_empty_and_none_deltas_are_ignored() -> None:
    a = StreamingTurnAssembler()
    a.append_text(None)
    a.append_text("")
    a.append_text("x")
    assert a.build().text == "x"


def test_reasoning_accumulates_separately_from_text() -> None:
    """Reasoning must never leak into the assistant text — it is not shown by default."""

    a = StreamingTurnAssembler()
    a.append_text("answer")
    a.append_reasoning("let me think")
    turn = a.build()
    assert turn.text == "answer"
    assert turn.reasoning == "let me think"


# ------------------------------------------------------------------ tool argument reassembly


def test_arguments_split_mid_json_are_parsed_once_at_the_end() -> None:
    """The fragments here are not individually valid JSON, and must never be parsed individually."""

    a = StreamingTurnAssembler()
    a.append_tool_name("search", index=0, tool_id="call_1")
    for fragment in ('{"qu', 'ery": "how ', "to ", 'test"', ', "limit": 5}'):
        a.append_tool_argument_fragment(fragment, index=0)
    call = a.build().tool_calls[0]
    assert call.complete is True
    assert call.name == "search"
    assert call.arguments == {"query": "how to test", "limit": 5}


def test_argument_split_inside_a_multibyte_character() -> None:
    """A UTF-8 codepoint split across chunks must survive reassembly."""

    a = StreamingTurnAssembler()
    a.append_tool_name("echo", index=0)
    a.append_tool_argument_fragment('{"text": "', index=0)
    a.append_tool_argument_fragment("öçşğü", index=0)
    a.append_tool_argument_fragment('"}', index=0)
    assert a.build().tool_calls[0].arguments == {"text": "öçşğü"}


def test_id_only_on_the_first_chunk_still_binds_later_fragments() -> None:
    """OpenAI sends the id once and the index on every chunk."""

    a = StreamingTurnAssembler()
    a.append_tool_name("f", index=0, tool_id="call_abc")
    a.append_tool_argument_fragment('{"a":', index=0)
    a.append_tool_argument_fragment("1}", index=0)
    call = a.build().tool_calls[0]
    assert call.id == "call_abc"
    assert call.arguments == {"a": 1}


def test_index_absent_but_id_present_binds_by_id() -> None:
    """Some providers send only the id after the first chunk."""

    a = StreamingTurnAssembler()
    a.register_tool_call(index=0, tool_id="call_x", name="f")
    a.append_tool_argument_fragment('{"a":', tool_id="call_x")
    a.append_tool_argument_fragment("2}", tool_id="call_x")
    assert a.build().tool_calls[0].arguments == {"a": 2}


def test_neither_index_nor_id_appends_to_the_most_recent_call() -> None:
    a = StreamingTurnAssembler()
    a.register_tool_call(index=0, name="f")
    a.append_tool_argument_fragment('{"a": ')
    a.append_tool_argument_fragment("3}")
    turn = a.build()
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].arguments == {"a": 3}


def test_parallel_calls_interleave_without_cross_contamination() -> None:
    a = StreamingTurnAssembler()
    a.append_tool_name("alpha", index=0, tool_id="c0")
    a.append_tool_name("beta", index=1, tool_id="c1")
    a.append_tool_argument_fragment('{"x"', index=0)
    a.append_tool_argument_fragment('{"y"', index=1)
    a.append_tool_argument_fragment(": 1}", index=0)
    a.append_tool_argument_fragment(": 2}", index=1)

    calls = a.build().tool_calls
    assert [c.name for c in calls] == ["alpha", "beta"]
    assert calls[0].arguments == {"x": 1}
    assert calls[1].arguments == {"y": 2}


def test_duplicate_tool_ids_across_indexes_stay_separate_calls() -> None:
    """A provider bug. Merging them would silently execute one call instead of two."""

    a = StreamingTurnAssembler()
    a.append_tool_name("alpha", index=0, tool_id="same")
    a.append_tool_name("beta", index=1, tool_id="same")
    a.append_tool_argument_fragment('{"x": 1}', index=0)
    a.append_tool_argument_fragment('{"y": 2}', index=1)

    calls = a.build().tool_calls
    assert len(calls) == 2
    assert calls[0].arguments == {"x": 1}
    assert calls[1].arguments == {"y": 2}


def test_streamed_tool_name_fragments_are_concatenated() -> None:
    a = StreamingTurnAssembler()
    a.append_tool_name("get_", index=0)
    a.append_tool_name("weather", index=0)
    a.append_tool_argument_fragment("{}", index=0)
    assert a.build().tool_calls[0].name == "get_weather"


def test_zero_argument_tool_call_is_complete() -> None:
    a = StreamingTurnAssembler()
    a.register_tool_call(index=0, name="now")
    turn = a.build()
    assert turn.tool_calls[0].complete is True
    assert turn.tool_calls[0].arguments == {}


# ------------------------------------------------------------------ malformed / interrupted


def test_incomplete_json_is_reported_not_dropped() -> None:
    """A cut-off call keeps its partial text so the failure can be explained."""

    a = StreamingTurnAssembler()
    a.append_tool_name("search", index=0)
    a.append_tool_argument_fragment('{"query": "unfin', index=0)
    a.mark_interrupted()

    turn = a.build()
    call = turn.tool_calls[0]
    assert call.complete is False
    assert "not valid JSON" in (call.error or "")
    assert call.raw_arguments == '{"query": "unfin'
    assert turn.finish_reason is FinishReason.INTERRUPTED
    assert turn.has_incomplete_tool_calls is True


def test_non_object_arguments_are_rejected() -> None:
    a = StreamingTurnAssembler()
    a.append_tool_name("f", index=0)
    a.append_tool_argument_fragment("[1, 2, 3]", index=0)
    call = a.build().tool_calls[0]
    assert call.complete is False
    assert "expected an object" in (call.error or "")


def test_tool_call_without_a_name_is_incomplete() -> None:
    a = StreamingTurnAssembler()
    a.append_tool_argument_fragment('{"a": 1}', index=0)
    call = a.build().tool_calls[0]
    assert call.complete is False
    assert "no name" in (call.error or "")


def test_cancellation_is_distinguished_from_an_interrupted_stream() -> None:
    a = StreamingTurnAssembler()
    a.append_text("partial")
    a.mark_interrupted(cancelled=True)
    assert a.build().finish_reason is FinishReason.CANCELLED


def test_oversized_arguments_are_truncated_and_flagged() -> None:
    a = StreamingTurnAssembler()
    a.append_tool_name("f", index=0)
    a.append_tool_argument_fragment('{"a": "' + "x" * (MAX_TOOL_ARGUMENT_BYTES + 10), index=0)

    turn = a.build()
    assert turn.truncated is True
    assert turn.tool_calls[0].complete is False
    assert "truncated" in (turn.tool_calls[0].error or "")
    assert len(turn.tool_calls[0].raw_arguments) <= MAX_TOOL_ARGUMENT_BYTES


def test_truncation_does_not_split_a_multibyte_character() -> None:
    a = StreamingTurnAssembler()
    a.append_tool_name("f", index=0)
    a.append_tool_argument_fragment("ö" * MAX_TOOL_ARGUMENT_BYTES, index=0)
    raw = a.build().tool_calls[0].raw_arguments
    raw.encode("utf-8")  # must not raise — no half-written codepoint


# ------------------------------------------------------------------ usage and finish reasons


def test_usage_from_a_final_chunk_is_kept() -> None:
    a = StreamingTurnAssembler()
    a.append_text("hi")
    a.set_usage(Usage(input_tokens=10, output_tokens=3, total_tokens=13))
    assert a.build().usage == Usage(input_tokens=10, output_tokens=3, total_tokens=13)


def test_a_later_none_usage_does_not_erase_a_recorded_one() -> None:
    a = StreamingTurnAssembler()
    a.set_usage(Usage(input_tokens=1))
    a.set_usage(None)
    assert a.build().usage == Usage(input_tokens=1)


def test_missing_usage_is_none_not_zero() -> None:
    """Zero tokens is a measurement; absent usage is not."""

    assert StreamingTurnAssembler().build().usage is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("stop", FinishReason.STOP),
        ("end_turn", FinishReason.STOP),
        ("max_tokens", FinishReason.LENGTH),
        ("length", FinishReason.LENGTH),
        ("tool_use", FinishReason.TOOL_CALLS),
        ("tool_calls", FinishReason.TOOL_CALLS),
        ("safety", FinishReason.CONTENT_FILTER),
        ("something_new", FinishReason.UNKNOWN),
    ],
)
def test_provider_finish_reasons_normalize(raw, expected) -> None:
    a = StreamingTurnAssembler()
    a.set_finish_reason(raw)
    assert a.build().finish_reason is expected


def test_finish_reason_defaults_to_tool_calls_when_calls_were_assembled() -> None:
    a = StreamingTurnAssembler()
    a.append_tool_name("f", index=0)
    a.append_tool_argument_fragment("{}", index=0)
    assert a.build().finish_reason is FinishReason.TOOL_CALLS


def test_keepalive_and_blank_fragments_do_not_create_phantom_calls() -> None:
    a = StreamingTurnAssembler()
    a.append_text("ok")
    a.append_tool_argument_fragment("")
    a.append_tool_argument_fragment(None)
    assert a.build().tool_calls == ()
