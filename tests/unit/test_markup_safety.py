"""Rich/Textual markup injection (item 14, item 22).

Anything the UI did not write itself — a model's message, a reasoning summary, a question, an answer,
a command, command output, a tool name, a file path, a provider error, an agent's own description —
is model- or attacker-controlled text. Rendered into a markup-enabled widget unescaped, it can forge
UI ("[green]✓ tests passed[/green]"), corrupt the rest of the render ("[/link]"), or push raw ANSI at
the terminal.

The previous code escaped only the question/answer of an ``ask_user`` event. These tests hold the line
for the whole surface.
"""

from __future__ import annotations

import re

from rich.text import Text

from openagent.core.events import EventType, NormalizedEvent
from openagent.core.projection import RunProjection
from openagent.tui.markup import safe_line, safe_markup

#: The payloads every escaping test uses.
HOSTILE = [
    "[b]fake success[/b]",
    "[/link]",
    "[red]injected[/red]",
    "<!-- marker -->",
    "[green]✓ all tests passed[/green]",
]

#: An opening bracket that Rich would treat as the start of a tag — i.e. one not backslash-escaped.
_LIVE_TAG = re.compile(r"(?<!\\)\[")


def assert_inert(rendered: str, original: str) -> None:
    """The rendered string must contain no live markup, yet still show the original text."""

    assert not _LIVE_TAG.search(rendered), f"live markup survived escaping: {rendered!r}"
    # Rich renders the escaped form back to the *literal* original — nothing is lost, nothing styled.
    plain = Text.from_markup(rendered).plain
    assert plain == original, f"escaping changed the visible text: {plain!r} != {original!r}"


def test_markup_is_escaped_not_interpreted():
    for payload in HOSTILE:
        assert_inert(safe_markup(payload), payload)


def test_ansi_and_control_characters_are_stripped():
    payload = "\x1b[31mred\x1b[0m\x07\x00 done"
    out = safe_markup(payload)
    assert "\x1b" not in out and "\x07" not in out and "\x00" not in out
    assert "red" in out and "done" in out


def test_limit_truncates_without_breaking_escapes():
    out = safe_markup("[red]" + "A" * 500, limit=20)
    assert len(out) <= 40  # escaping adds a backslash; the bound still holds
    assert out.endswith("…")


def test_safe_line_collapses_newlines():
    assert safe_line("a\nb\tc  d") == "a b c d"


def test_none_renders_empty():
    assert safe_markup(None) == ""
    assert safe_line(None) == ""


# --------------------------------------------------------------------------- through the projection


def _event(etype: EventType, **data) -> NormalizedEvent:
    return NormalizedEvent(run_id="run_1", type=etype, source="codex-cli", data=data)


def test_hostile_text_survives_projection_and_is_escaped_at_render():
    """A hostile payload can reach every projected field; the console escapes each one (item 14)."""

    payload = "[green]✓ approved[/green][/link]"
    projection = RunProjection("run_1")
    projection.apply(_event(EventType.REASONING_SUMMARY, item_id="r1", text=payload))
    projection.apply(_event(EventType.MESSAGE_COMPLETED, item_id="m1", text=payload))
    projection.apply(
        _event(
            EventType.COMMAND_COMPLETED, item_id="c1", command=payload, output=payload, exit_code=0
        )
    )
    projection.apply(_event(EventType.FILE_MODIFIED, item_id="f1", path=payload, change="modified"))
    projection.apply(
        _event(EventType.PLAN_UPDATED, item_id="p1", items=[{"text": payload, "completed": False}])
    )
    projection.apply(_event(EventType.WEB_SEARCH_COMPLETED, item_id="w1", query=payload))
    projection.apply(_event(EventType.TOOL_COMPLETED, item_id="t1", tool=payload))

    # The projection stores the raw text (it is data, and events.jsonl is the source of truth)…
    assert projection.reasoning[0].text == payload
    # …and every render path neutralizes it.
    for value in (
        projection.reasoning[0].text,
        projection.messages[0].text,
        projection.commands[0].command,
        projection.commands[0].output,
        projection.files[0].path,
        projection.plan[0].text,
        projection.web_searches[0].query,
        projection.by_kind("tool")[0].tool,
    ):
        assert_inert(safe_markup(value), payload)
