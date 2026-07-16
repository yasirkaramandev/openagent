"""Control-flow and progress tools (spec §2.1, item 12).

``ask_user`` surfaces a question (routed through the approval gate / callback); ``finish_task``
signals the agent loop to stop with a final summary.

``update_plan`` and ``report_progress`` are how an **OpenAgent-owned API agent** tells the user what
it is doing while it works — the same transparency a CLI backend gets for free from its own event
stream. They are *explicit, user-facing statements the model chooses to publish*, not an extraction of
hidden reasoning: OpenAgent never asks for, infers, or stores private chain-of-thought (item 1).
"""

from __future__ import annotations

from typing import Any

from .base import ToolContext, ToolResult

#: Bound on published progress text, so a runaway model can't flood the console or the artifacts.
MAX_SUMMARY_CHARS = 1_000
MAX_PLAN_ITEMS = 30
MAX_PLAN_ITEM_CHARS = 200


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
            question=question,
            answered=False,
        )

    answer = answer.strip()
    if ctx.emit:
        ctx.emit("question.answered", {"question": question, "answer": answer})
    return ToolResult.success(answer, question=question, answered=True)


def finish_task(ctx: ToolContext, summary: str) -> ToolResult:
    raise TaskFinished(summary)


def update_plan(ctx: ToolContext, items: list[Any] | None = None) -> ToolResult:
    """Publish the agent's current checklist so the user can see the plan and its progress (item 12).

    Maps onto ``plan.updated`` — the same normalized event a Codex ``todo_list`` produces, so the Run
    Console renders an API agent's plan and a CLI agent's plan identically. Re-calling it *replaces*
    the plan (the projection keys it by item id), which is why it carries the whole checklist rather
    than a delta.
    """

    entries: list[dict[str, Any]] = []
    for raw in (items or [])[:MAX_PLAN_ITEMS]:
        if isinstance(raw, dict):
            text = str(raw.get("text") or "").strip()
            completed = bool(raw.get("completed"))
        else:
            text, completed = str(raw).strip(), False
        if text:
            entries.append({"text": text[:MAX_PLAN_ITEM_CHARS], "completed": completed})

    if not entries:
        return ToolResult.failure("update_plan needs a non-empty 'items' list")

    if ctx.emit:
        # A stable item id: the plan is one thing that changes, not a stream of new plans (item 3).
        ctx.emit("plan.updated", {"item_id": "plan", "items": entries})
    done = sum(1 for e in entries if e["completed"])
    return ToolResult.success(f"plan updated ({done}/{len(entries)} complete)")


def report_progress(ctx: ToolContext, summary: str, next_step: str = "") -> ToolResult:
    """Publish a short, user-visible progress statement (item 12).

    This is the agent explaining itself *to the user* — what it found, what it is doing, what happens
    next. It is emitted as ``progress.updated`` and shown in the Run Console under "Reasoning
    summary", alongside a CLI backend's own summaries. It is **not** a channel for private
    chain-of-thought, and the system prompt says so explicitly.
    """

    text = (summary or "").strip()
    if not text:
        return ToolResult.failure("report_progress needs a non-empty 'summary'")
    payload = {"summary": text[:MAX_SUMMARY_CHARS]}
    if next_step.strip():
        payload["next_step"] = next_step.strip()[:MAX_SUMMARY_CHARS]
    if ctx.emit:
        ctx.emit("progress.updated", payload)
    return ToolResult.success("progress reported")
