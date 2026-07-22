"""OPENAGENT.md generation (spec §33).

The SQLite DB is the source of truth; ``OPENAGENT.md`` is generated from it. The agent list lives
between two markers so regeneration never disturbs hand-written prose around it.

Two things this module has to get right, because the file is jointly owned: OpenAgent writes the
block between the markers, and the user writes everything else. Losing the user's half is not
recoverable from the database.

**Concurrency.** ``atomic_write_text`` guarantees the file is never observed half-written, which is
durability, not concurrency. Two processes that each read, regenerate, and replace will still lose
one of the two edits — the second ``os.replace`` simply wins. Regeneration therefore happens under
a cross-process lock, and the file is re-checked after the new content is built: if it changed
while we were working, the write is abandoned rather than applied over a stale read.

**Malformed markers.** The previous implementation fell back to rendering a fresh document whenever
it could not find both markers. A file with a ``BEGIN`` but no ``END`` — a truncated write, a bad
merge resolution, a hand-edit — took that path, and the fallback replaced the *entire* file,
deleting every line of hand-written prose. That case is now a refusal: ``OpenAgentMdConflict`` names
the problem and the user decides.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from pathlib import Path

from ..config import OPENAGENT_MD_END, OPENAGENT_MD_START
from ..core.models import AgentProfile
from ..security.atomic import atomic_write_text
from ..security.file_lock import LockTimeout, file_lock


class OpenAgentMdConflict(RuntimeError):
    """The document is not in a shape that can be regenerated without guessing.

    Always actionable and never destructive: nothing is written when this is raised.
    """

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(
            f"{path} cannot be regenerated safely: {reason}. "
            f"Run `openagent agent sync-document --dry-run` to see the document OpenAgent would "
            f"write, then fix the markers or replace the file yourself."
        )


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


def _sanitize(text: str) -> str:
    """Neutralize agent-supplied text so it cannot break the generated document (item 14).

    Every user field (name, title, description, tags) renders on a single markdown line inside the
    marker-delimited block. Untrusted text is:

    * collapsed to one line (no injected headings/list items via newlines);
    * defanged of HTML-comment syntax and the OPENAGENT markers, so it can never inject a comment
      or forge/truncate the ``OPENAGENT:AGENTS:START/END`` sentinels the regenerator splits on;
    * stripped of backticks so inline-code spans stay balanced.

    The SQLite DB remains the source of truth for the real values; this only guards the rendering.
    """

    if not text:
        return text
    text = " ".join(text.split())
    text = text.replace("<!--", "< !--").replace("-->", "-- >")
    text = text.replace("OPENAGENT:AGENTS:START", "OPENAGENT-AGENTS-START")
    text = text.replace("OPENAGENT:AGENTS:END", "OPENAGENT-AGENTS-END")
    text = text.replace("`", "'")
    return text


def render_agents_block(agents: Sequence[AgentProfile]) -> str:
    if not agents:
        body = "\n_No agents registered yet. Add one with `openagent add`._\n"
    else:
        parts: list[str] = [""]
        for agent in sorted(agents, key=lambda a: a.name):
            runtime = _runtime_label(agent)
            tags = ", ".join(f"`{_sanitize(t)}`" for t in agent.tags) or "—"
            parts.append(f"### {_sanitize(agent.title) or _sanitize(agent.name)}")
            parts.append("")
            parts.append(f"- Name: `{_sanitize(agent.name)}`")
            parts.append(f"- Runtime: `{runtime}`")
            parts.append(f"- Tags: {tags}")
            parts.append(f"- Description: {_sanitize(agent.description) or '—'}")
            parts.append("")
        body = "\n".join(parts)
    return f"{OPENAGENT_MD_START}\n{body}\n{OPENAGENT_MD_END}\n"


def render_document(agents: Sequence[AgentProfile]) -> str:
    return f"{_HEADER}\n{render_agents_block(agents)}"


#: How long to wait for another process to finish regenerating the document. Generous, because the
#: work is milliseconds and a wait means someone genuinely holds it; bounded, because a crashed
#: holder's lock is already released by the OS and we must never wait forever on a live one.
DOCUMENT_LOCK_TIMEOUT = 30.0


def document_lock_path(path: Path) -> Path:
    """The lock guarding ``path``. Kept beside the project, not in a shared temp directory."""

    return path.parent / ".openagent" / "locks" / "openagent-md.lock"


def _preimage(path: Path) -> tuple[int, int, str] | None:
    """(size, mtime_ns, sha256) of ``path``, or None when it does not exist.

    ``lstat``, not ``stat``: a symlink must be detected as a symlink rather than followed, or a
    regenerate would write through it to a target the user never nominated.
    """

    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return (info.st_size, info.st_mtime_ns, digest)


def plan_openagent_md(path: Path, agents: Sequence[AgentProfile]) -> str:
    """The content that would be written, or raise :class:`OpenAgentMdConflict`.

    Separated from the write so ``--dry-run`` shows exactly what would land, and so the conflict
    rules are stated once rather than duplicated between preview and apply.
    """

    block = render_agents_block(agents)
    if not path.exists():
        return render_document(agents)

    if path.is_symlink():
        raise OpenAgentMdConflict(path, "it is a symlink, and OpenAgent will not write through one")

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise OpenAgentMdConflict(path, "it is not valid UTF-8") from exc

    starts = text.count(OPENAGENT_MD_START)
    ends = text.count(OPENAGENT_MD_END)

    if starts == 0 and ends == 0:
        # A document with prose but no generated block yet: append rather than replace, so a user
        # who deleted the block (or wrote the file themselves) keeps everything they wrote.
        separator = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return f"{text}{separator}{block}"

    # Every remaining shape is ambiguous. Rendering a fresh document here is what deleted users'
    # prose before v0.1.6 — the fallback replaced the entire file.
    if starts != 1 or ends != 1:
        raise OpenAgentMdConflict(
            path,
            f"it contains {starts} start marker(s) and {ends} end marker(s); exactly one of each "
            "is required",
        )
    if text.index(OPENAGENT_MD_START) > text.index(OPENAGENT_MD_END):
        raise OpenAgentMdConflict(path, "the end marker appears before the start marker")

    before = text.split(OPENAGENT_MD_START)[0]
    after = text.split(OPENAGENT_MD_END, 1)[1]
    return before + block + after


def write_openagent_md(
    path: Path, snapshot: Callable[[], Sequence[AgentProfile]] | Sequence[AgentProfile]
) -> None:
    """Create or update ``OPENAGENT.md`` in place, preserving prose outside the markers.

    Held under a cross-process lock, and guarded by a compare-and-swap on the file's own state: the
    content is read, the replacement computed, and the file re-examined immediately before the
    replace. If anything changed in between — another OpenAgent process, or the user saving in an
    editor — the write is abandoned and retried against the new content rather than applied over a
    stale read.

    ``snapshot`` is a callable read **inside** the lock, not a pre-fetched list (spec §10). The DB is
    the source of truth, so the committed agent set the document reflects must be sampled after the
    document lock is held — otherwise a snapshot taken before the lock could be regenerated over a
    document another process wrote from a newer commit, resurrecting agents the DB no longer has. A
    plain sequence is still accepted for callers (like ``--dry-run``) that already hold their own
    consistent read.

    Raises :class:`OpenAgentMdConflict` when the document is in a shape that cannot be regenerated
    without guessing. Nothing is written in that case.
    """

    lock = document_lock_path(path)
    try:
        with file_lock(lock, timeout=DOCUMENT_LOCK_TIMEOUT):
            # Sample the committed DB snapshot only now that the lock is held (spec §10 ordering).
            agents = snapshot() if callable(snapshot) else snapshot
            for _attempt in range(3):
                before = _preimage(path)
                content = plan_openagent_md(path, agents)
                if _preimage(path) != before:
                    # Changed while we were rendering. Rebuild against what is there now; the loop
                    # is bounded because an unbounded retry against a file someone is actively
                    # editing would never terminate.
                    continue
                atomic_write_text(path, content, mode=0o600)
                return
            raise OpenAgentMdConflict(
                path, "it kept changing while OpenAgent tried to regenerate it"
            )
    except LockTimeout as exc:
        raise OpenAgentMdConflict(
            path, "another OpenAgent process is already regenerating it"
        ) from exc
    except OSError as exc:
        if not isinstance(exc, FileNotFoundError):
            raise OpenAgentMdConflict(path, f"its lock could not be taken ({exc})") from exc
        raise


def _runtime_label(agent: AgentProfile) -> str:
    rt = agent.runtime
    rtype = rt.type if isinstance(rt.type, str) else rt.type.value
    if rtype == "cli":
        return f"{rt.cli}-cli" if rt.cli else "cli"
    return "api"
