"""Initial-context builder for API agents (spec §27).

The whole repository is never sent. The first turn carries the agent's system prompt, the task, a
short project summary, a shallow file list, and (if present) the ``OPENAGENT.md`` guidance. The
model pulls anything else it needs via ``read_file`` / ``search_text``.
"""

from __future__ import annotations

import os
from pathlib import Path

from ...core.models import AgentProfile
from ...providers.base import Message, Role

_IGNORE = {".git", ".openagent", ".venv", "node_modules", "__pycache__", ".mypy_cache"}
_MAX_FILES = 100

_WORKING_RULES = """\
Working rules:
- Make minimal, testable changes. Prefer apply_patch over write_file.
- Read files before editing them. Do not assume file contents.
- Use run_tests to verify your changes when possible.
- When finished, call finish_task with a concise summary of what changed.

Keeping the user informed:
- The user is watching a live console. Once you have a plan, call update_plan with your checklist,
  and call it again as steps complete.
- Before each major phase, call report_progress with a brief, user-visible summary: what you found,
  what you are doing, and what happens next.
- Do not reveal private chain-of-thought. Report conclusions and actions, not internal deliberation.
- Do not narrate every trivial tool call; a few well-placed updates are better than a running
  commentary."""

#: Used only when the caller doesn't supply the real workspace description (item 17).
_DEFAULT_WORKSPACE_NOTE = (
    "You are working inside an isolated workspace; the user reviews your diff."
)


def build_system_prompt(agent: AgentProfile, workspace_note: str = "") -> str:
    note = workspace_note.strip() or _DEFAULT_WORKSPACE_NOTE
    body = f"{_WORKING_RULES}\n- {note}"
    base = agent.system_prompt.strip()
    return f"{base}\n\n{body}" if base else body


def build_initial_messages(agent: AgentProfile, prompt: str, workspace_root: Path) -> list[Message]:
    summary = _project_summary(workspace_root)
    task = f"Task:\n{prompt}\n\n{summary}"
    return [Message(role=Role.USER, content=task)]


def _project_summary(root: Path) -> str:
    files = _list_files(root)
    lines = ["Project files (shallow list; use read_file/search_text for more):"]
    lines.extend(f"  {f}" for f in files)
    guidance = _read_optional(root / "OPENAGENT.md") or _read_optional(root / "README.md")
    if guidance:
        lines.append("\nProject guidance (excerpt):")
        lines.append(guidance[:1500])
    return "\n".join(lines)


def _list_files(root: Path) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE]
        for name in sorted(filenames):
            rel = (Path(dirpath) / name).relative_to(root)
            out.append(str(rel))
            if len(out) >= _MAX_FILES:
                return out
    return out


def _read_optional(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
