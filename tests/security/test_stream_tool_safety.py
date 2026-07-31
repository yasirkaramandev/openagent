"""Tool calls are only executable when the provider said the turn finished (spec §7).

A tool call is a side effect: it writes files, runs commands, spends money. Every other check in
the pipeline validates *what* the call says — these validate whether the provider ever finished
saying it. Two independent failures make a syntactically perfect tool call unsafe to run:

* the stream stopped before a terminal event, so the "call" may be a prefix of something else;
* two fragments claimed the same call with contradictory identity, so we cannot tell which of the
  two the arguments belong to.

Both are fail-closed here. A turn that is not demonstrably terminal contributes no executable
calls at all, and a contradictory identity poisons every call in the turn rather than the one that
happened to collide — a stream that lies about identity once has not earned trust for the rest.
"""

from __future__ import annotations

import json

from pytest_httpx import HTTPXMock

from openagent.core.errors import ErrorType
from openagent.providers.base import Message, ModelEventType, NormalizedModelRequest, Role
from openagent.providers.compat.profiles_v2 import get_profile
from openagent.providers.streaming import FinishReason, StreamingTurnAssembler
from openagent.providers.transport import Transport
from openagent.providers.wire.openai_chat import OpenAIChatWire

BASE = "https://api.test/v1"


def wire(profile=None, **kwargs) -> OpenAIChatWire:
    return OpenAIChatWire(
        profile=profile or get_profile("deepseek"),
        transport=Transport(base_url=BASE, headers={"Content-Type": "application/json"}),
        **kwargs,
    )


def req(stream: bool = True, **kwargs) -> NormalizedModelRequest:
    fields = {
        "model": "test-model",
        "system": "be brief",
        "messages": [Message(role=Role.USER, content="hi")],
        "stream": stream,
    }
    fields.update(kwargs)
    return NormalizedModelRequest(**fields)  # type: ignore[arg-type]


def data(*chunks: dict) -> str:
    """SSE body WITHOUT a terminal marker — the stream simply stops."""

    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)


#: One complete, well-formed, immediately-executable tool call.
COMPLETE_CALL = {
    "choices": [
        {
            "delta": {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "ping", "arguments": '{"value": 1}'},
                    }
                ]
            }
        }
    ]
}


# --------------------------------------------------------------------- terminal evidence


class TestATurnMustBeTerminalBeforeItsToolsRun:
    async def test_interrupted_stream_executes_no_tools(self, httpx_mock: HTTPXMock) -> None:
        """The arguments parse and the name is valid — and it still must not run.

        The provider never sent a finish reason, so this "complete" call is indistinguishable from
        the first of two calls whose second never arrived, or from a call the model was about to
        revise. Executing it acts on a sentence the model had not finished saying.
        """

        httpx_mock.add_response(
            text=data(COMPLETE_CALL),
            headers={"content-type": "text/event-stream"},
        )
        w = wire()
        events = [e async for e in w.events(req())]

        assert w.last_turn is not None
        assert w.last_turn.finish_reason is FinishReason.INTERRUPTED
        assert [e for e in events if e.type is ModelEventType.TOOL_CALL] == []
        errors = [e for e in events if e.type is ModelEventType.ERROR]
        assert errors, "an interrupted turn holding tool calls must report why it was refused"
        assert errors[0].error_type == ErrorType.STREAM_INTERRUPTED.value

    async def test_cancelled_stream_executes_no_tools(self) -> None:
        """Cancellation is a terminal *reason*, but not a terminal *turn*."""

        assembler = StreamingTurnAssembler()
        assembler.register_tool_call(index=0, tool_id="call_1", name="ping")
        assembler.append_tool_argument_fragment('{"value": 1}', index=0, tool_id="call_1")
        assembler.mark_interrupted(cancelled=True)

        turn = assembler.build()
        assert turn.finish_reason is FinishReason.CANCELLED
        assert turn.tool_calls, "the parsed call is still visible for diagnosis"
        assert turn.executable_tool_calls == ()

    async def test_a_terminal_turn_still_executes_its_tools(self, httpx_mock: HTTPXMock) -> None:
        """The guard must not swallow the ordinary case it exists to protect."""

        finished = json.loads(json.dumps(COMPLETE_CALL))
        finished["choices"][0]["finish_reason"] = "tool_calls"
        httpx_mock.add_response(
            text=data(finished) + "data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )
        w = wire()
        events = [e async for e in w.events(req())]

        calls = [e for e in events if e.type is ModelEventType.TOOL_CALL]
        assert [c.tool_call.name for c in calls if c.tool_call] == ["ping"]


# --------------------------------------------------------------------- identity conflicts


class TestContradictoryIdentityPoisonsTheWholeTurn:
    def test_duplicate_tool_id_different_index_rejects_all(self) -> None:
        """One id cannot name two calls. Which one do the arguments belong to?"""

        a = StreamingTurnAssembler()
        a.register_tool_call(index=0, tool_id="call_1", name="ping")
        a.register_tool_call(index=1, tool_id="call_1", name="pong")
        a.set_finish_reason("tool_calls")

        turn = a.build()
        assert turn.protocol_violations
        assert turn.executable_tool_calls == ()

    def test_same_index_different_tool_id_rejects_all(self) -> None:
        a = StreamingTurnAssembler()
        a.register_tool_call(index=0, tool_id="call_1", name="ping")
        a.register_tool_call(index=0, tool_id="call_2", name="ping")
        a.set_finish_reason("tool_calls")

        turn = a.build()
        assert turn.protocol_violations
        assert turn.executable_tool_calls == ()

    def test_a_conflict_poisons_calls_that_did_not_collide(self) -> None:
        """A stream that contradicts itself once is not trusted for its other calls either."""

        a = StreamingTurnAssembler()
        a.register_tool_call(index=0, tool_id="call_ok", name="safe")
        a.append_tool_argument_fragment("{}", index=0, tool_id="call_ok")
        a.register_tool_call(index=1, tool_id="call_x", name="ping")
        a.register_tool_call(index=2, tool_id="call_x", name="pong")
        a.set_finish_reason("tool_calls")

        turn = a.build()
        assert len(turn.tool_calls) >= 2
        assert turn.executable_tool_calls == ()

    def test_changing_a_settled_tool_name_is_a_violation(self) -> None:
        a = StreamingTurnAssembler()
        a.register_tool_call(index=0, tool_id="call_1", name="ping")
        a.set_tool_name(index=0, tool_id="call_1", name="rm_rf")
        a.set_finish_reason("tool_calls")

        turn = a.build()
        assert turn.protocol_violations
        assert turn.executable_tool_calls == ()


# --------------------------------------------------------------------- name fragmentation


class TestToolNameFragmentSemantics:
    def test_repeated_full_tool_name_is_not_concatenated(self) -> None:
        """OpenAI-chat repeats the whole name on each chunk; blind concatenation yields "pingping"."""

        a = StreamingTurnAssembler(tool_names_are_fragments=False)
        a.append_tool_name("ping", index=0, tool_id="call_1")
        a.append_tool_name("ping", index=0, tool_id="call_1")
        a.append_tool_argument_fragment("{}", index=0, tool_id="call_1")
        a.set_finish_reason("tool_calls")

        turn = a.build()
        assert [c.name for c in turn.tool_calls] == ["ping"]
        assert turn.executable_tool_calls, "a repeated identical name is not a conflict"

    def test_fragmented_tool_name_still_concatenates(self) -> None:
        """Anthropic-style deltas really do split a name; that mode must survive the fix."""

        a = StreamingTurnAssembler(tool_names_are_fragments=True)
        a.append_tool_name("pi", index=0, tool_id="call_1")
        a.append_tool_name("ng", index=0, tool_id="call_1")
        a.append_tool_argument_fragment("{}", index=0, tool_id="call_1")
        a.set_finish_reason("tool_calls")

        assert [c.name for c in a.build().tool_calls] == ["ping"]


# --------------------------------------------------------------------- the executing loop


class TestTheApiLoopHonoursTheGate:
    async def test_the_wire_emits_an_error_not_a_tool_call(self, httpx_mock: HTTPXMock) -> None:
        """End-to-end: the loop that executes tools must never receive the call at all."""

        httpx_mock.add_response(
            text=data(COMPLETE_CALL),
            headers={"content-type": "text/event-stream"},
        )
        w = wire()
        types = [e.type async for e in w.events(req())]
        assert ModelEventType.TOOL_CALL not in types
        assert ModelEventType.ERROR in types
