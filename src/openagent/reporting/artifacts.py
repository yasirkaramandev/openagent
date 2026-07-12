"""Standard run artifact bundle (spec §35).

Whichever runtime did the work, every run directory ends up with the same files so downstream tools
(TUI, CLI, other agents via MCP) read one shape:

``request.json  status.json  events.jsonl  output.md  result.json  logs.txt  changes.diff
tests.json  handoff.md``

(``events.jsonl`` is written incrementally by the event log.)

Every artifact is passed through :func:`redact` before it hits disk — including the user prompt in
``request.json`` and the ``changes.diff`` (a diff can easily contain a pasted secret). Files are
written with owner-only permissions where the platform supports it.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..core.models import Run
from ..credentials.redaction import redact

_IS_WINDOWS = sys.platform.startswith("win")


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
        status = run.status if isinstance(run.status, str) else run.status.value
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

    def write_results(self, run: Run, art: RunArtifacts) -> None:
        status = run.status if isinstance(run.status, str) else run.status.value
        # Scrub free text and the diff before anything hits disk (spec §30).
        art.summary = redact(art.summary)
        art.warnings = [redact(w) for w in art.warnings]
        self._json("result.json", {
            "run_id": run.id,
            "status": status,
            "agent": run.agent,
            "turns": run.turns,
            "summary": art.summary,
            "files_changed": art.files_changed,
            "tests": art.tests.to_dict(),
            "warnings": art.warnings,
            "usage": art.usage,
            "session_id": run.provider_session_id,
        })
        self._json("tests.json", art.tests.to_dict())
        self._text("changes.diff", redact(art.diff))
        self._text("logs.txt", redact("\n".join(art.log_lines)))
        self._text("output.md", redact(_render_output_md(run, art)))
        self._text("handoff.md", redact(_render_handoff_md(run, art)))

    def write_turn(self, run: Run, prompt: str, art: RunArtifacts) -> None:
        """Record a resume turn's outcome as an explicit ``turn_NNN.md`` artifact (spec §32)."""

        status = run.status if isinstance(run.status, str) else run.status.value
        lines = [
            f"# Turn {run.turns} — {run.id}", "",
            f"- Status: {status}", "",
            "## Prompt", "", redact(prompt), "",
            "## Summary", "", redact(art.summary) or "(no summary)", "",
        ]
        self._text(f"turn_{run.turns:03d}.md", "\n".join(lines))

    def _json(self, name: str, data: dict) -> None:
        self._text(name, json.dumps(data, indent=2))

    def _text(self, name: str, text: str) -> None:
        path = self.run_dir / name
        path.write_text(text, encoding="utf-8")
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
    status = run.status if isinstance(run.status, str) else run.status.value
    lines = ["# Run Result", "", "## Summary", "", art.summary or "(no summary)", ""]
    lines += ["## Status", "", f"- Status: {status}", f"- Agent: {run.agent}", f"- Turns: {run.turns}", ""]
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


def _render_handoff_md(run: Run, art: RunArtifacts) -> str:
    status = run.status if isinstance(run.status, str) else run.status.value
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
