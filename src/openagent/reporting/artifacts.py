"""Standard run artifact bundle (spec §35).

Whichever runtime did the work, every run directory ends up with the same files so downstream tools
(TUI, CLI, other agents via MCP) read one shape:

``request.json  status.json  events.jsonl  output.md  result.json  logs.txt  changes.diff
tests.json  handoff.md  timeline.md``

(``events.jsonl`` is written incrementally by the event log.)

``timeline.md`` is the human-readable narrative of the run (item 23): per turn, the reasoning
summaries the backend published, the plan as it evolved, the commands, the files, the tests, and the
final result. ``result.json`` carries the same material in structured form. Neither contains hidden
chain-of-thought — only what the backend itself exposed as user-visible.

Every artifact is passed through :func:`redact` before it hits disk — including the user prompt in
``request.json`` and the ``changes.diff`` (a diff can easily contain a pasted secret). Files are
written with owner-only permissions where the platform supports it. Untrusted text (command output,
model messages) is bounded before it is written.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..core.models import Run, enum_value
from ..core.projection import RunProjection
from ..credentials.redaction import redact

_IS_WINDOWS = sys.platform.startswith("win")

#: Bounds on untrusted content in the artifact bundle (item 23).
MAX_OUTPUT_CHARS = 4_000       # per command, in timeline.md
MAX_TEXT_CHARS = 4_000         # per message / reasoning summary
MAX_TIMELINE_COMMANDS = 200


@dataclass
class TestSummary:
    ran: bool = False
    passed: bool | None = None
    exit_code: int | None = None
    command: str = ""

    def to_dict(self) -> dict:
        return {"ran": self.ran, "passed": self.passed, "exit_code": self.exit_code,
                "command": self.command}


@dataclass
class RunArtifacts:
    summary: str = ""
    changes: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    diff: str = ""
    tests: TestSummary = field(default_factory=TestSummary)
    warnings: list[str] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    #: The normalized failure (error_type / message / phase / source) captured from ``run.failed``,
    #: so the reason a run died reaches output.md and not just the event log (item 13).
    error: dict = field(default_factory=dict)


class ArtifactWriter:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        _secure_dir(self.run_dir)

    def write_request(self, run: Run) -> None:
        # The user prompt can itself contain a pasted secret — redact it (spec §30).
        self._json("request.json", {
            "run_id": run.id,
            "agent": run.agent,
            "prompt": redact(run.prompt),
            "workspace": run.workspace,
            "worktree": run.worktree,
            "worktree_strategy": run.worktree_strategy,
            "permission_profile": run.permission_profile,
        })

    def write_status(self, run: Run) -> None:
        status = enum_value(run.status)
        self._json("status.json", {
            "run_id": run.id,
            "status": status,
            "turns": run.turns,
            "started_at": run.started_at.isoformat(),
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "exit_code": run.exit_code,
            "failure_type": redact(run.failure_type) if run.failure_type else None,
            "session_id": run.provider_session_id,
        })

    def write_results(
        self, run: Run, art: RunArtifacts, projection: RunProjection | None = None
    ) -> None:
        status = enum_value(run.status)
        # Scrub free text and the diff before anything hits disk (spec §30).
        art.summary = redact(art.summary)
        art.warnings = [redact(w) for w in art.warnings]
        result = {
            "run_id": run.id,
            "status": status,
            "phase": run.phase,
            "agent": run.agent,
            "turns": run.turns,
            "summary": art.summary,
            "files_changed": art.files_changed,
            "tests": art.tests.to_dict(),
            "warnings": art.warnings,
            "usage": art.usage,
            "session_id": run.provider_session_id,
            "failure_type": redact(run.failure_type) if run.failure_type else None,
        }
        result.update(_structured(projection))
        self._json("result.json", result)
        self._json("tests.json", art.tests.to_dict())
        self._text("changes.diff", redact(art.diff))
        self._text("logs.txt", redact("\n".join(art.log_lines)))
        self._text("output.md", redact(_render_output_md(run, art)))
        self._text("handoff.md", redact(_render_handoff_md(run, art)))

    def write_timeline(self, run: Run, projection: RunProjection) -> None:
        """The narrative of the run: what the agent said, planned, ran, and changed (item 23)."""

        self._text("timeline.md", redact(_render_timeline_md(run, projection)))

    def write_turn(
        self, run: Run, prompt: str, art: RunArtifacts,
        event_range: tuple[int, int] | None = None,
    ) -> None:
        """Record a resume turn as an explicit ``turn_NNN.md`` artifact (spec §32, item 18).

        Scoped to THIS turn only: its prompt, summary, usage, tests, and the range of events it
        produced. The cumulative view of the whole run lives in ``result.json``.
        """

        status = enum_value(run.status)
        usage = art.usage or {}
        usage_line = (
            f"in {usage.get('input_tokens', 0)} / cached {usage.get('cached_input_tokens', 0)} / "
            f"out {usage.get('output_tokens', 0)}"
        )
        tests = art.tests
        tests_line = (
            f"ran `{tests.command}` → {'passed' if tests.passed else 'failed'} "
            f"(exit {tests.exit_code})" if tests.ran else "no tests run this turn"
        )
        events_line = (
            f"events {event_range[0]}–{event_range[1]}" if event_range else "(range unavailable)"
        )
        lines = [
            f"# Turn {run.turns} — {run.id}", "",
            f"- Status: {status}", f"- Usage: {usage_line}", f"- Tests: {tests_line}",
            f"- Events: {events_line}", "",
            "## Prompt", "", redact(prompt), "",
            "## Summary", "", redact(art.summary) or "(no summary)", "",
        ]
        self._text(f"turn_{run.turns:03d}.md", "\n".join(lines))

    def _json(self, name: str, data: dict) -> None:
        self._text(name, json.dumps(data, indent=2))

    def _text(self, name: str, text: str) -> None:
        # Atomic write (item 9.4): render into a sibling temp file, then ``os.replace`` it into place.
        # A reader (the TUI, the CLI, another agent) therefore only ever sees a *complete* artifact —
        # never a half-written result.json — even if the process dies mid-write or the disk fills.
        path = self.run_dir / name
        tmp = path.with_name(f".{name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            _secure_file(tmp)
            os.replace(tmp, path)  # atomic on POSIX and Windows when src/dst share a directory
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        _secure_file(path)


def _secure_file(path: Path) -> None:
    if not _IS_WINDOWS:
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass


def _secure_dir(path: Path) -> None:
    if not _IS_WINDOWS:
        try:
            os.chmod(path, 0o700)
        except OSError:  # pragma: no cover - platform dependent
            pass


def _render_output_md(run: Run, art: RunArtifacts) -> str:
    status = enum_value(run.status)
    lines = ["# Run Result", "", "## Summary", "", art.summary or "(no summary)", ""]
    lines += ["## Status", "", f"- Status: {status}", f"- Agent: {run.agent}", f"- Turns: {run.turns}", ""]
    if status != "completed" and (art.error or run.failure_type):
        # Why it failed, in the artifact a human opens first (item 13).
        error = art.error
        lines += ["## Failure", "",
                  f"- Type: {error.get('error_type') or run.failure_type}",
                  f"- Phase: {error.get('phase') or run.phase}",
                  f"- Source: {error.get('source') or 'openagent'}"]
        if error.get("message"):
            lines.append(f"- Message: {str(error['message'])[:MAX_TEXT_CHARS]}")
        lines.append("")
    if art.warnings:
        lines += ["## Warnings", ""]
        lines += [f"- {w}" for w in art.warnings]
        lines.append("")
    if art.changes:
        lines += ["## Changes", ""]
        lines += [f"- {c}" for c in art.changes]
        lines.append("")
    lines += ["## Tests", ""]
    if art.tests.ran:
        verdict = "passed" if art.tests.passed else "failed"
        lines.append(f"- Tests {verdict} (exit {art.tests.exit_code})")
    else:
        lines.append("- No tests run")
    lines.append("")
    lines += ["## Files Changed", ""]
    lines += [f"- {f}" for f in art.files_changed] or ["- (none)"]
    lines.append("")
    return "\n".join(lines)


def _structured(projection: RunProjection | None) -> dict:
    """The structured view of a run for ``result.json`` (item 23).

    Everything here came from the backend's own user-visible stream: reasoning **summaries** (never
    hidden reasoning), the plan it published, the commands it ran, the searches it made, and the
    turns it took. All free text is bounded.
    """

    # ``turns`` stays the integer count (it means the same thing in status.json); the per-turn
    # structure lives under ``turn_details`` so the two never collide.
    if projection is None:
        return {"reasoning_summaries": [], "plan": [], "commands": [], "web_searches": [],
                "files": [], "turn_details": []}
    return {
        "reasoning_summaries": [
            {"item_id": i.item_id, "source": i.source, "turn": i.turn,
             "text": i.text[:MAX_TEXT_CHARS]}
            for i in projection.reasoning if i.text
        ],
        "plan": [p.to_dict() for p in projection.plan],
        "commands": [
            {"item_id": i.item_id, "command": i.command, "status": i.status,
             "exit_code": i.exit_code, "turn": i.turn,
             "output": i.output[:MAX_OUTPUT_CHARS]}
            for i in projection.commands[:MAX_TIMELINE_COMMANDS]
        ],
        "web_searches": [
            {"item_id": i.item_id, "query": i.query, "status": i.status}
            for i in projection.web_searches
        ],
        "files": [
            {"path": i.path, "change": i.change, "status": i.status}
            for i in projection.files
        ],
        "turn_details": [
            {"number": t.number, "prompt": t.prompt[:MAX_TEXT_CHARS], "status": t.status,
             "started_at": t.started_at}
            for t in sorted(projection.turns.values(), key=lambda t: t.number)
        ],
    }


def _render_timeline_md(run: Run, projection: RunProjection) -> str:
    status = enum_value(run.status)
    lines = [
        f"# Timeline — {run.id}", "",
        f"- Agent: {run.agent}",
        f"- Status: {status} (phase: {run.phase})",
        f"- Turns: {run.turns}",
        f"- Workspace: {run.worktree or run.workspace}",
        f"- Permission profile: {run.permission_profile}",
    ]
    if run.provider_session_id:
        lines.append(f"- Session: {run.provider_session_id}")
    if projection.usage:
        usage = projection.usage
        lines.append(
            f"- Usage: in {usage.get('input_tokens', 0)} / cached "
            f"{usage.get('cached_input_tokens', 0)} / out {usage.get('output_tokens', 0)} / "
            f"reasoning {usage.get('reasoning_tokens', 0)}"
        )
    lines.append("")

    by_turn: dict[int, list] = {}
    for item in projection.items:
        by_turn.setdefault(item.turn, []).append(item)

    for number in sorted(by_turn):
        turn = projection.turns.get(number)
        lines.append(f"## Turn {number}")
        lines.append("")
        if turn and turn.prompt:
            lines += ["**Prompt**", "", turn.prompt[:MAX_TEXT_CHARS], ""]
        for item in by_turn[number]:
            lines += _timeline_entry(item)
        lines.append("")

    if projection.plan:
        lines += ["## Final plan", ""]
        lines += [f"- [{'x' if p.completed else ' '}] {p.text}" for p in projection.plan]
        lines.append("")
    if projection.error:
        err = projection.error
        lines += ["## Failure", "",
                  f"- Type: {err.get('error_type')}",
                  f"- Phase: {err.get('phase') or '(unknown)'}",
                  f"- Source: {err.get('source')}",
                  f"- Message: {str(err.get('message'))[:MAX_TEXT_CHARS]}", ""]
    final = projection.final_message
    if final:
        lines += ["## Final response", "", final[:MAX_TEXT_CHARS], ""]
    return "\n".join(lines)


def _timeline_entry(item) -> list[str]:
    mark = {"completed": "✓", "failed": "✗", "cancelled": "○", "in_progress": "●"}.get(
        item.status, "•"
    )
    if item.kind in ("reasoning", "progress"):
        label = "Reasoning summary" if item.kind == "reasoning" else "Progress"
        return [f"- {mark} **{label}**: {item.text[:MAX_TEXT_CHARS]}"]
    if item.kind == "plan":
        out = [f"- {mark} **Plan**"]
        out += [f"    - [{'x' if p.completed else ' '}] {p.text}" for p in item.plan]
        return out
    if item.kind == "command":
        head = f"- {mark} `{item.command}` (exit {item.exit_code})"
        if not item.output:
            return [head]
        body = item.output[:MAX_OUTPUT_CHARS].rstrip()
        return [head, "", "  ```", *[f"  {line}" for line in body.splitlines()], "  ```"]
    if item.kind == "file":
        return [f"- {mark} {item.change} `{item.path}`"]
    if item.kind == "web_search":
        return [f"- {mark} Web search: {item.query}"]
    if item.kind == "tool":
        return [f"- {mark} Tool `{item.title or item.tool}`"]
    if item.kind == "message":
        return [f"- {mark} **Message**: {item.text[:MAX_TEXT_CHARS]}"]
    return []


def _render_handoff_md(run: Run, art: RunArtifacts) -> str:
    status = enum_value(run.status)
    lines = [
        f"# Handoff — {run.id}", "",
        f"Agent `{run.agent}` finished with status **{status}** after {run.turns} turn(s).", "",
        "## What was done", "", art.summary or "(no summary)", "",
        "## Files changed", "",
    ]
    lines += [f"- {f}" for f in art.files_changed] or ["- (none)"]
    lines += ["", "## Next steps", "",
              "- Review `changes.diff` and apply/merge/discard the worktree.",
              f"- Resume with `openagent message --id {run.id} -p \"...\"` if supported.", ""]
    return "\n".join(lines)
