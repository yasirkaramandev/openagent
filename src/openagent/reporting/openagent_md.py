"""OPENAGENT.md generation (spec §33).

The SQLite DB is the source of truth; ``OPENAGENT.md`` is generated from it. The agent list lives
between two markers so regeneration never disturbs hand-written prose around it.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from ..config import OPENAGENT_MD_END, OPENAGENT_MD_START
from ..core.models import AgentProfile

_HEADER = """\
# OpenAgent

This repository uses OpenAgent to discover and run external AI agents.

## Instructions for AI Assistants

1. Run `openagent list --json` to discover available agents.
2. Delegate work with:
   `openagent run --name <name> --prompt "<task>" --worktree auto`
3. Retrieve a result with:
   `openagent output --id <run-id> --format json`
4. Never request or expose credentials.
5. Use isolated worktrees for file-changing tasks.

## Available Agents
"""


def render_agents_block(agents: Sequence[AgentProfile]) -> str:
    if not agents:
        body = "\n_No agents registered yet. Add one with `openagent add`._\n"
    else:
        parts: list[str] = [""]
        for agent in sorted(agents, key=lambda a: a.name):
            runtime = _runtime_label(agent)
            tags = ", ".join(f"`{t}`" for t in agent.tags) or "—"
            parts.append(f"### {agent.title or agent.name}")
            parts.append("")
            parts.append(f"- Name: `{agent.name}`")
            parts.append(f"- Runtime: `{runtime}`")
            parts.append(f"- Tags: {tags}")
            parts.append(f"- Description: {agent.description or '—'}")
            parts.append("")
        body = "\n".join(parts)
    return f"{OPENAGENT_MD_START}\n{body}\n{OPENAGENT_MD_END}\n"


def render_document(agents: Sequence[AgentProfile]) -> str:
    return f"{_HEADER}\n{render_agents_block(agents)}"


def write_openagent_md(path: Path, agents: Sequence[AgentProfile]) -> None:
    """Create or update ``OPENAGENT.md`` in place, preserving prose outside the markers.

    The write is **atomic** (temp file + ``os.replace``): a failure mid-write never leaves a
    half-updated document, and the previous content survives if the write fails. Only the block
    between the two markers is regenerated; hand-written prose outside them is preserved.
    """

    block = render_agents_block(agents)
    content: str | None = None
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if OPENAGENT_MD_START in text and OPENAGENT_MD_END in text:
            before = text.split(OPENAGENT_MD_START)[0]
            after = text.split(OPENAGENT_MD_END, 1)[1]
            content = before + block + after
    if content is None:
        content = render_document(agents)
    _atomic_write(path, content)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".openagent-md-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:  # pragma: no cover - best effort cleanup
            pass
        raise


def _runtime_label(agent: AgentProfile) -> str:
    rt = agent.runtime
    rtype = rt.type if isinstance(rt.type, str) else rt.type.value
    if rtype == "cli":
        return f"{rt.cli}-cli" if rt.cli else "cli"
    return "api"
