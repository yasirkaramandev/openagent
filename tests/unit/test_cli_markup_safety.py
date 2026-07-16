"""The CLI event renderer escapes model/command-controlled values (item 12).

The TUI Run Console was already hardened, but the CLI's ``_print_event`` still interpolated tool
names, commands, file paths, and failure messages straight into Rich markup strings — so a payload
like ``[green]✓ done[/green]`` emitted by a model would forge a success line, and a stray ``[/link]``
would raise a markup error mid-render. These tests hold the line for the CLI surface.
"""

from __future__ import annotations

import pytest

from openagent.cli.app import _print_event, console
from openagent.core.events import EventType, NormalizedEvent

HOSTILE = ["[green]fake success[/green]", "[/link]", "[red]injected[/red]"]


def _render(event: NormalizedEvent) -> str:
    with console.capture() as cap:
        _print_event(event)
    return cap.get()


@pytest.mark.parametrize("payload", HOSTILE)
@pytest.mark.parametrize(
    ("etype", "key"),
    [
        (EventType.TOOL_COMPLETED, "tool"),
        (EventType.TOOL_FAILED, "tool"),
        (EventType.COMMAND_STARTED, "command"),
        (EventType.FILE_MODIFIED, "path"),
        (EventType.RUN_FAILED, "message"),
    ],
)
def test_event_values_are_escaped_not_interpreted(payload: str, etype: EventType, key: str) -> None:
    # If the markup were interpreted, Rich would consume the tags (or raise on the stray [/link]) and
    # the literal brackets would be gone. Their survival — with no exception — proves it was escaped.
    out = _render(NormalizedEvent(run_id="r", type=etype, source="x", data={key: payload}))
    assert payload in out


def test_forged_success_line_does_not_become_a_real_completed_marker() -> None:
    out = _render(
        NormalizedEvent(
            run_id="r",
            type=EventType.TOOL_COMPLETED,
            source="x",
            data={"tool": "[green]● completed[/green]"},
        )
    )
    # The genuine completed marker is produced only by a real run.completed event; a tool name that
    # merely *looks* like one stays inert literal text.
    assert "[green]● completed[/green]" in out
