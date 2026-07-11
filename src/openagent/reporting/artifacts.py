"""Standard run artifact bundle (spec §35).

Whichever runtime did the work, every run directory ends up with the same files so downstream tools
(TUI, CLI, other agents via MCP) read one shape:

``request.json  status.json  events.jsonl  output.md  result.json  logs.txt  changes.diff
tests.json  handoff.md``

(``events.jsonl`` is written incrementally by the event log.)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..core.models import Run
from ..credentials.redaction import redact


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

    def write_request(self, run: Run) -> None:
        self._json("request.json", {
            "run_id": run.id,
            "agent": run.agent,
            "prompt": run.prompt,
            "workspace": run.workspace,
            "worktree": run.worktree,
            "permission_profile": run.permission_profile,
        })

    def write_status(self, run: Run) -> None:
        status = run.status if isinstance(run.status, str) else run.status.value
        self._json("status.json", {
            "run_id": run.id,
            "status": status,
            "started_at": run.started_at.isoformat(),
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "exit_code": run.exit_code,
            "failure_type": run.failure_type,
            "session_id": run.provider_session_id,
        })

    def write_results(self, run: Run, art: RunArtifacts) -> None:
        status = run.status if isinstance(run.status, str) else run.status.value
        # Model-produced free text may echo a secret; scrub before it hits disk (spec §30).
        art.summary = redact(art.summary)
        art.warnings = [redact(w) for w in art.warnings]
        self._json("result.json", {
            "run_id": run.id,
            "status": status,
            "agent": run.agent,
            "summary": art.summary,
            "files_changed": art.files_changed,
            "tests": art.tests.to_dict(),
            "warnings": art.warnings,
            "usage": art.usage,
            "session_id": run.provider_session_id,
        })
        self._json("tests.json", art.tests.to_dict())
        (self.run_dir / "changes.diff").write_text(art.diff, encoding="utf-8")
        (self.run_dir / "logs.txt").write_text(
            redact("\n".join(art.log_lines)), encoding="utf-8"
        )
        (self.run_dir / "output.md").write_text(_render_output_md(run, art), encoding="utf-8")
        (self.run_dir / "handoff.md").write_text(_render_handoff_md(run, art), encoding="utf-8")

    def _json(self, name: str, data: dict) -> None:
        (self.run_dir / name).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _render_output_md(run: Run, art: RunArtifacts) -> str:
    status = run.status if isinstance(run.status, str) else run.status.value
    lines = ["# Run Result", "", "## Summary", "", art.summary or "(no summary)", ""]
    lines += ["## Status", "", f"- Status: {status}", f"- Agent: {run.agent}", ""]
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
        f"Agent `{run.agent}` finished with status **{status}**.", "",
        "## What was done", "", art.summary or "(no summary)", "",
        "## Files changed", "",
    ]
    lines += [f"- {f}" for f in art.files_changed] or ["- (none)"]
    lines += ["", "## Next steps", "",
              "- Review `changes.diff` and apply/merge/discard the worktree.",
              f"- Resume with `openagent message --id {run.id} -p \"...\"` if supported.", ""]
    return "\n".join(lines)
