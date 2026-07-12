"""Control-flow tools (spec §2.1).

``ask_user`` surfaces a question (routed through the approval gate / callback); ``finish_task``
signals the agent loop to stop with a final summary.
"""

from __future__ import annotations

from .base import ToolContext, ToolResult


class TaskFinished(Exception):
    """Raised by ``finish_task`` to end the agent loop cleanly."""

    def __init__(self, summary: str) -> None:
        super().__init__(summary)
        self.summary = summary


def ask_user(ctx: ToolContext, question: str) -> ToolResult:
    """Ask the interactive user a question and return their answer as the tool result (item 16).

    Emits the dedicated **question** lifecycle — ``question.requested`` → ``question.answered`` /
    ``question.cancelled`` — never ``approval.requested``, which stays reserved for permission
    decisions (item 13). Under the TUI a real modal opens and the run waits for the answer; in a
    non-interactive run (no resolver, or the user cancels) the tool emits ``question.cancelled`` and
    falls back to best judgment. The question and answer are recorded on the event stream, where
    secret redaction applies before anything hits disk (spec §30).
    """

    if ctx.emit:
        ctx.emit("question.requested", {"question": question})

    answer = ctx.ask_user_callback(question) if ctx.ask_user_callback is not None else None
    if answer is None or not answer.strip():
        reason = "no interactive user available" if ctx.ask_user_callback is None else "cancelled"
        if ctx.emit:
            ctx.emit("question.cancelled", {"question": question, "reason": reason})
        return ToolResult.success(
            "No interactive user is available; proceed with your best judgment and note assumptions.",
            question=question, answered=False,
        )

    answer = answer.strip()
    if ctx.emit:
        ctx.emit("question.answered", {"question": question, "answer": answer})
    return ToolResult.success(answer, question=question, answered=True)


def finish_task(ctx: ToolContext, summary: str) -> ToolResult:
    raise TaskFinished(summary)
