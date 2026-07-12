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

    Under the TUI a real modal opens and the run waits for the answer. In a non-interactive run
    (no resolver, or the user cancels) the tool falls back to best-judgment and says so. Both the
    question and the answer are recorded on the event stream, where secret redaction applies.
    """

    if ctx.emit:
        ctx.emit("approval.requested", {"kind": "question", "question": question})

    answer = ctx.ask_user_callback(question) if ctx.ask_user_callback is not None else None
    if answer is None or not answer.strip():
        if ctx.emit:
            ctx.emit("log", {"kind": "question_unanswered", "question": question})
        return ToolResult.success(
            "No interactive user is available; proceed with your best judgment and note assumptions.",
            question=question, answered=False,
        )

    answer = answer.strip()
    if ctx.emit:
        # Redaction is applied by the event log before this hits disk (spec §30).
        ctx.emit("log", {"kind": "question_answered", "question": question, "answer": answer})
    return ToolResult.success(answer, question=question, answered=True)


def finish_task(ctx: ToolContext, summary: str) -> ToolResult:
    raise TaskFinished(summary)
