"""One streaming accumulator for every provider (spec §11).

Every streaming adapter has to solve the same problem: a turn arrives as fragments, and the
fragments are hostile. Tool-call arguments come as JSON split at arbitrary byte offsets — not at
token boundaries, not at valid-JSON boundaries. Indexes identify which of several parallel calls a
fragment belongs to, except when the provider reuses an id, or sends the id only on the first
chunk, or sends no index at all. Usage arrives in a final chunk that may never come. The stream can
stop mid-argument because the model was cancelled, the connection dropped, or the provider decided
it was done.

Written per-adapter, this becomes the same subtly-different bug six times: one adapter concatenates
by index and breaks on duplicate ids, another keys by id and breaks when the id is absent, a third
parses arguments per fragment and silently drops a call whose JSON was split across a chunk. So it
is written once, here, and adapters feed it events.

Two rules the whole design rests on:

* **Never parse a fragment.** Arguments are accumulated as text and parsed once, at build time.
  A fragment is not JSON and asking whether it is will eventually say yes to something malformed.
* **Incomplete is a state, not a failure.** A turn interrupted mid-argument yields a tool call
  marked incomplete with its partial text preserved, so the caller can report *what* was cut off.
  Silently dropping it, or emitting it as though it were complete, both lose the same information.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

#: Hard ceiling on accumulated argument text per tool call. A provider looping on a fragment must
#: not be able to grow this unboundedly — the process has to stay alive to report the problem.
MAX_TOOL_ARGUMENT_BYTES = 1_048_576
#: Ceiling on accumulated reasoning text. Reasoning is not replayed to the user by default and a
#: runaway reasoning stream is the same memory hazard.
MAX_REASONING_BYTES = 4_194_304
#: Ceiling on accumulated assistant text.
MAX_TEXT_BYTES = 8_388_608


class FinishReason(str, Enum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


@dataclass
class ToolCallDraft:
    """A tool call being assembled. ``arguments_text`` is raw and unparsed until :meth:`build`."""

    index: int
    id: str | None = None
    name: str | None = None
    arguments_text: str = ""
    truncated: bool = False

    def append_arguments(self, fragment: str) -> None:
        if self.truncated:
            return
        remaining = MAX_TOOL_ARGUMENT_BYTES - len(self.arguments_text.encode("utf-8", "ignore"))
        if remaining <= 0:
            self.truncated = True
            return
        encoded = fragment.encode("utf-8", "ignore")
        if len(encoded) > remaining:
            # Cut on a character boundary, never mid-codepoint: a half-written multi-byte character
            # would corrupt the text we are trying to preserve for diagnosis.
            self.arguments_text += encoded[:remaining].decode("utf-8", "ignore")
            self.truncated = True
            return
        self.arguments_text += fragment


@dataclass(frozen=True)
class ToolCall:
    """A finished tool call. ``complete`` is False when arguments did not parse."""

    index: int
    id: str | None
    name: str | None
    arguments: dict[str, Any]
    raw_arguments: str
    complete: bool
    error: str | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class AssembledTurn:
    text: str
    reasoning: str
    tool_calls: tuple[ToolCall, ...]
    usage: Usage | None
    finish_reason: FinishReason
    #: True when any accumulator hit its ceiling — the turn is usable but not the whole story.
    truncated: bool = False

    @property
    def has_incomplete_tool_calls(self) -> bool:
        return any(not call.complete for call in self.tool_calls)


class StreamingTurnAssembler:
    """Accumulate one assistant turn from streamed fragments (spec §11).

    Not thread-safe and not reusable: one instance per turn.
    """

    def __init__(self) -> None:
        self._text: list[str] = []
        self._text_bytes = 0
        self._reasoning: list[str] = []
        self._reasoning_bytes = 0
        self._tools: dict[int, ToolCallDraft] = {}
        self._by_id: dict[str, int] = {}
        self._order: list[int] = []
        self._usage: Usage | None = None
        self._finish: FinishReason | None = None
        self._truncated = False

    # ------------------------------------------------------------------ text / reasoning

    def append_text(self, delta: str | None) -> None:
        if not delta:
            return
        size = len(delta.encode("utf-8", "ignore"))
        if self._text_bytes + size > MAX_TEXT_BYTES:
            self._truncated = True
            return
        self._text.append(delta)
        self._text_bytes += size

    def append_reasoning(self, delta: str | None) -> None:
        if not delta:
            return
        size = len(delta.encode("utf-8", "ignore"))
        if self._reasoning_bytes + size > MAX_REASONING_BYTES:
            self._truncated = True
            return
        self._reasoning.append(delta)
        self._reasoning_bytes += size

    # ------------------------------------------------------------------ tool calls

    def _draft(self, *, index: int | None, tool_id: str | None) -> ToolCallDraft:
        """Resolve which call a fragment belongs to, from whatever the provider gave us.

        Providers disagree about what identifies a call mid-stream. Some send an index on every
        chunk and the id once; some send the id every time and no index; some send neither after
        the first chunk. The id wins when present and previously seen, because it is the only
        globally meaningful handle; otherwise the index does; otherwise the fragment belongs to the
        most recent call, which is the only remaining interpretation that is ever right.
        """

        if tool_id is not None and tool_id in self._by_id:
            return self._tools[self._by_id[tool_id]]
        if index is None:
            if tool_id is None and self._order:
                return self._tools[self._order[-1]]
            index = len(self._order)
            while index in self._tools:
                index += 1
        draft = self._tools.get(index)
        if draft is None:
            draft = ToolCallDraft(index=index)
            self._tools[index] = draft
            self._order.append(index)
        if tool_id is not None and draft.id is None:
            draft.id = tool_id
            # A duplicate id across two indexes is a provider bug. The first binding wins; the
            # second keeps its own slot rather than merging two distinct calls into one.
            self._by_id.setdefault(tool_id, draft.index)
        return draft

    def append_tool_name(
        self, name: str | None, *, index: int | None = None, tool_id: str | None = None
    ) -> None:
        draft = self._draft(index=index, tool_id=tool_id)
        if not name:
            return
        # Names may also stream in fragments (rare, but Anthropic-style deltas can split them).
        draft.name = name if draft.name is None else draft.name + name

    def append_tool_argument_fragment(
        self, fragment: str | None, *, index: int | None = None, tool_id: str | None = None
    ) -> None:
        # An empty fragment with no index and no id carries no information at all — it is a
        # keep-alive, a blank SSE line, or a delta object with an empty arguments string. Resolving
        # a draft for it would invent a tool call the provider never announced, which then surfaces
        # as a nameless incomplete call in the finished turn. With an explicit index or id it *is*
        # information: the provider is declaring the call exists before its arguments arrive.
        if not fragment and index is None and tool_id is None:
            return
        draft = self._draft(index=index, tool_id=tool_id)
        if fragment:
            draft.append_arguments(fragment)

    def register_tool_call(
        self, *, index: int | None = None, tool_id: str | None = None, name: str | None = None
    ) -> None:
        """Declare a call exists before any arguments arrive, preserving parallel-call ordering."""

        draft = self._draft(index=index, tool_id=tool_id)
        if name and draft.name is None:
            draft.name = name

    # ------------------------------------------------------------------ terminal fields

    def set_usage(self, usage: Usage | None) -> None:
        if usage is not None:
            self._usage = usage

    def set_finish_reason(self, reason: FinishReason | str | None) -> None:
        if reason is None:
            return
        if isinstance(reason, FinishReason):
            self._finish = reason
            return
        try:
            self._finish = FinishReason(reason)
        except ValueError:
            self._finish = _FINISH_ALIASES.get(reason.strip().lower(), FinishReason.UNKNOWN)

    def mark_interrupted(self, *, cancelled: bool = False) -> None:
        """The stream ended without a finish reason — a drop, a timeout, or a cancellation."""

        self._finish = FinishReason.CANCELLED if cancelled else FinishReason.INTERRUPTED

    # ------------------------------------------------------------------ result

    def build(self) -> AssembledTurn:
        """Parse accumulated fragments once and produce the finished turn."""

        calls: list[ToolCall] = []
        for index in self._order:
            draft = self._tools[index]
            calls.append(_finish_tool_call(draft))

        finish = self._finish
        if finish is None:
            finish = FinishReason.TOOL_CALLS if calls else FinishReason.UNKNOWN

        return AssembledTurn(
            text="".join(self._text),
            reasoning="".join(self._reasoning),
            tool_calls=tuple(calls),
            usage=self._usage,
            finish_reason=finish,
            truncated=self._truncated or any(d.truncated for d in self._tools.values()),
        )


_FINISH_ALIASES = {
    "end_turn": FinishReason.STOP,
    "stop_sequence": FinishReason.STOP,
    "max_tokens": FinishReason.LENGTH,
    "tool_use": FinishReason.TOOL_CALLS,
    "function_call": FinishReason.TOOL_CALLS,
    "content_filtered": FinishReason.CONTENT_FILTER,
    "safety": FinishReason.CONTENT_FILTER,
}


def _finish_tool_call(draft: ToolCallDraft) -> ToolCall:
    raw = draft.arguments_text
    if draft.truncated:
        return ToolCall(
            index=draft.index,
            id=draft.id,
            name=draft.name,
            arguments={},
            raw_arguments=raw,
            complete=False,
            error=f"arguments exceeded {MAX_TOOL_ARGUMENT_BYTES} bytes and were truncated",
        )
    stripped = raw.strip()
    if not stripped:
        # A call with no arguments is legitimate — a zero-arg tool sends "" or "{}".
        return ToolCall(
            index=draft.index,
            id=draft.id,
            name=draft.name,
            arguments={},
            raw_arguments=raw,
            complete=draft.name is not None,
            error=None if draft.name is not None else "tool call has no name",
        )
    try:
        parsed = json.loads(stripped)
    except ValueError as exc:
        return ToolCall(
            index=draft.index,
            id=draft.id,
            name=draft.name,
            arguments={},
            raw_arguments=raw,
            complete=False,
            error=f"arguments are not valid JSON ({exc.__class__.__name__})",
        )
    if not isinstance(parsed, dict):
        return ToolCall(
            index=draft.index,
            id=draft.id,
            name=draft.name,
            arguments={},
            raw_arguments=raw,
            complete=False,
            error=f"arguments parsed to {type(parsed).__name__}, expected an object",
        )
    return ToolCall(
        index=draft.index,
        id=draft.id,
        name=draft.name,
        arguments=parsed,
        raw_arguments=raw,
        complete=draft.name is not None,
        error=None if draft.name is not None else "tool call has no name",
    )
