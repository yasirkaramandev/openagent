"""Escaping for anything the UI did not write itself (item 14).

Rich/Textual widgets with ``markup=True`` interpret ``[...]`` as styling. Every string that reaches
one of them from *outside* — a model message, a reasoning summary, a question, an answer, a command,
command output, a tool name, a file path, a provider error, a CLI error, an agent's name or
description — is attacker- or model-controlled text. Unescaped, it can:

* forge UI: ``[green]✓ tests passed[/green]`` in a model's message renders as a real success line;
* break rendering: an unclosed ``[/link]`` corrupts everything after it;
* smuggle control characters into the terminal.

So: one helper, used everywhere, rather than remembering to escape at each call site. Where markup is
not needed at all, the widget is created with ``markup=False`` instead — that is stronger, and this
helper is for the places that *do* mix trusted markup with untrusted values.
"""

from __future__ import annotations

import re

from rich.markup import escape

from ..credentials.redaction import redact

#: Control characters (including ANSI CSI introducers) that must never reach the terminal. Command
#: output routinely contains ANSI colour codes; rendered raw they would repaint the console.
_CONTROL = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

DEFAULT_ELLIPSIS = "…"


def strip_control(value: str) -> str:
    """Remove ANSI escape sequences and other control characters (keeping tab/newline)."""

    return _CONTROL.sub("", value)


def safe_display(value: object, *, limit: int | None = None, single_line: bool = False) -> str:
    """The one way to put an externally supplied value on screen (spec §8).

    Escaping is not redaction. This helper used to escape markup and strip control characters but
    never redact, so a provider echoing the API key back in an error body had that key rendered —
    escaped, and therefore *safe to display*, which is precisely the wrong guarantee. The artifact
    writers called ``redact()`` and were fine; the ~129 UI call sites went through here and were not.

    The order below is deliberate and is the whole point of centralising this:

    1. **Strip control/ANSI first.** The terminal renders ``sk-abcd\\x1b[0mEFGH…`` as one continuous
       key. Redacting first would see two short fragments, match neither, and stripping afterwards
       would reassemble the secret on screen. Normalise to what will actually be shown, *then* look
       for secrets in it.
    2. **Redact**, on that normalised text.
    3. **Collapse to one line** if asked (tables, labels).
    4. **Truncate** — after redaction, never before, or the limit slices a key mid-pattern and leaves
       a readable prefix.
    5. **Escape markup** last, so the escaping applies to the final rendered text.
    """

    if value is None:
        return ""
    text = strip_control(str(value))
    text = redact(text)
    if limit is not None and len(text) > limit:
        text = text[: max(0, limit - 1)] + DEFAULT_ELLIPSIS
    if single_line:
        text = " ".join(text.split())
    escaped = escape(text)
    # Rich's escaper intentionally leaves unknown-looking tags alone. Textual's Static renderer is
    # more permissive and can consequently consume an uppercase placeholder such as ``[REDACTED]``
    # as markup, making the fact that a secret was removed invisible. Every value entering this
    # helper is untrusted text, so escape any opening bracket Rich left behind as well.
    return re.sub(r"(?<!\\)\[", r"\\[", escaped)


def safe_markup(value: object, limit: int | None = None) -> str:
    """Render ``value`` as text that is safe to place inside a markup-enabled widget.

    Thin alias for :func:`safe_display`; kept because it is already the habit at every call site, and
    routing it here is what gives all of them redaction rather than editing 129 places.
    """

    return safe_display(value, limit=limit)


def safe_line(value: object, limit: int | None = None) -> str:
    """Like :func:`safe_markup`, but collapsed to a single line (for tables and one-line labels)."""

    return safe_display(value, limit=limit, single_line=True)
