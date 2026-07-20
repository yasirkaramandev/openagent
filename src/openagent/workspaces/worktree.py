"""Worktree management (spec §28).

Three explicit isolation strategies:

* ``auto`` — a git repo gets an isolated git *worktree* (branch ``openagent/run_<id>``); a non-git
  project falls back to an isolated directory **copy**, flagged lower-safety.
* ``none`` — run directly in the current project directory (no isolation). File-editing agents
  require explicit confirmation before this is used (enforced by the caller).
* ``copy`` — always an isolated directory copy, regardless of git.

For copies (and non-git ``none`` runs) there is no git to diff against, so changed/created/deleted
files and a unified diff are computed by comparing the workspace to the untouched source tree.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..core.limits import RUNTIME_LIMITS
from ..security.atomic import atomic_write_text
from ..security.filesystem import SafeWorkspaceWalker, UnsafeWorkspacePath, safe_rmtree
from ..security.git_runner import GIT, GitError, GitMissing, GitTimeout

# Re-exported so existing importers keep working. The classes are *defined* in security/git_runner
# now, because that is where git subprocesses are executed — a caller catching GitError from either
# module is catching the same class, not two that happen to share a name.
__all__ = [
    "AUTO",
    "COPY",
    "NONE",
    "STRATEGIES",
    "GitError",
    "GitMissing",
    "GitTimeout",
    "Workspace",
    "WorktreeManager",
    "git_available",
    "is_git_repo",
]

_IGNORE_DIRS = {
    ".git",
    ".openagent",
    ".venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
}
_MAX_DIFF_BYTES = RUNTIME_LIMITS.diff_bytes

AUTO = "auto"
NONE = "none"
COPY = "copy"
STRATEGIES = (AUTO, NONE, COPY)


#: Every git call is bounded (spec §10). git can block indefinitely — an index.lock held by another
#: process, a credential/pager prompt, a network remote — and an unbounded call would hang the whole
#: run with no diagnosis.
GIT_TIMEOUT = 60


def _git(args: list[str], cwd: Path, *, timeout: int = GIT_TIMEOUT) -> str:
    """Run a git command through the hardened runner and return its stdout.

    Every property this function used to implement inline now comes from
    :mod:`openagent.security.git_runner`: a bounded timeout with **process-tree** termination (the
    old ``subprocess.run`` killed only the direct child, leaving helpers holding ``index.lock``
    despite the docstring claiming otherwise), no inherited parent environment, and no
    repository-supplied hook, pager, credential helper or diff driver.

    The old implementation passed ``{**os.environ, ...}``, which handed every provider API key in
    the parent process to a child that a checked-out ``.git/hooks/pre-commit`` could take over.
    """

    return GIT.inspect(args, cwd, timeout=timeout).stdout


def git_available() -> bool:
    """Whether a usable git exists on PATH (spec §10)."""

    return shutil.which("git") is not None


def _porcelain_paths(raw: str) -> list[str]:
    """Parse ``git status --porcelain=v1 -z`` into paths (spec §10).

    NUL-delimited, because the human-readable format is genuinely ambiguous: a filename containing a
    space parsed fine by accident, but git *quotes* names with specials (``"a\\tb"``), and a rename
    is emitted as ``R  old -> new`` — so slicing ``line[3:]`` produced a mangled path, and a filename
    containing a newline broke the line split entirely. With ``-z`` each entry is
    ``XY <path>\\0`` and a rename adds a second ``<origin>\\0`` record, with no quoting at all.
    """

    paths: list[str] = []
    fields = raw.split("\0")
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        # An entry is `XY <path>`: the status is exactly two columns wide and the path starts at 3.
        # It must be sliced by position, not split on whitespace — either column may itself be a
        # space (` M foo` is "modified, unstaged"), and paths legitimately contain spaces.
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if not path:
            continue
        paths.append(path)
        # A rename/copy emits its ORIGIN path as the very next NUL-terminated field. Consume it, or
        # it would be misread as the next status entry. The R/C code can land in either column
        # (staged vs. worktree), so check both.
        if "R" in status or "C" in status:
            index += 1
    return paths


def _untracked_files(root: Path) -> list[str]:
    """Untracked, non-ignored files — read-only, so the index is never written (spec §10)."""

    raw = _git(["ls-files", "--others", "--exclude-standard", "-z"], root)
    return [p for p in raw.split("\0") if p]


def _untracked_diff(root: Path, rel: str) -> str:
    """A diff for one untracked file via ``--no-index`` (compares paths; never uses the index)."""

    try:
        SafeWorkspaceWalker(root).read_bytes(rel, max_bytes=1)
    except (OSError, UnsafeWorkspacePath):
        return ""
    try:
        # --no-index exits 1 when the files differ, which is the normal case here, so the non-zero
        # return is expected rather than an error. This path went through a second, separately
        # written subprocess.run that also inherited os.environ — the isolation fix has to cover
        # both call sites or it covers neither.
        return GIT.diff(["--no-index", "--", os.devnull, rel], root, check=False)
    except GitError:
        # Includes GitMissing and GitTimeout. An untracked file we cannot diff contributes nothing
        # to the report; it is never a reason to fail the run.
        return ""


def is_git_repo(path: Path) -> bool:
    """Whether ``path`` is inside a git work tree.

    Returns False when git is missing or hangs, rather than propagating: a machine without git must
    still be able to run agents (the `auto` strategy falls back to a copy workspace). Previously only
    ``GitError`` was caught, so a missing git raised FileNotFoundError straight out of
    ``subprocess.run`` and crashed run setup.
    """

    try:
        out = _git(["rev-parse", "--is-inside-work-tree"], path)
        return out.strip() == "true"
    except GitError:  # GitMissing and GitTimeout are subclasses
        return False


@dataclass
class Workspace:
    """A prepared place for a run to work in."""

    run_id: str
    root: Path  # where the agent runs (worktree dir, or copy, or the repo itself)
    source: Path  # the user's project root
    is_git: bool
    strategy: str = AUTO
    branch: str | None = None
    base_commit: str | None = None
    is_copy: bool = False  # True when using an isolated directory copy
    in_place: bool = False  # True for strategy "none" (runs in the source tree directly)
    #: For non-git diffing (copies and in-place non-git runs): an **immutable snapshot** of the
    #: source tree captured at creation, before the agent touched anything. The diff compares the
    #: workspace against this snapshot — persisted across resume so the baseline never drifts and a
    #: resumed turn's created/modified/deleted files are computed correctly (item 5). ``None`` for
    #: git-diffed runs, which use the base commit instead.
    baseline_dir: Path | None = None

    @property
    def lower_safety(self) -> bool:
        # A copy is unversioned; running in place mutates the user's tree directly.
        return self.is_copy or self.in_place

    def describe_for_agent(self) -> str:
        """A truthful one-line description of where the agent is writing (item 17).

        The API-agent system prompt must not always claim "isolated worktree": the real strategy
        determines the safety story, and ``none`` mode writes to the user's actual project.
        """

        if self.in_place:
            return (
                "You are working DIRECTLY in the user's project directory — there is NO isolation. "
                "Every edit changes their real files immediately, so be minimal and careful."
            )
        if self.is_copy:
            return (
                "You are working in an isolated COPY of the project (not a git worktree). Your "
                "changes stay in the copy; the user reviews the diff before applying it."
            )
        return (
            "You are working inside an isolated git worktree on a scratch branch. Your changes do "
            "not touch the user's working tree; they review the diff afterward."
        )


class WorktreeManager:
    def __init__(self, project_root: Path, worktrees_dir: Path) -> None:
        self.project_root = project_root
        self.worktrees_dir = worktrees_dir

    def create(
        self, run_id: str, *, strategy: str = AUTO, use_worktree: bool | None = None
    ) -> Workspace:
        """Prepare an isolated workspace for ``run_id`` using an explicit strategy.

        ``use_worktree`` is a back-compat shim: ``False`` maps to ``strategy="none"``.
        """

        if use_worktree is False:
            strategy = NONE
        if strategy not in STRATEGIES:
            raise ValueError(f"unknown worktree strategy {strategy!r}; choose from {STRATEGIES}")

        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        git = is_git_repo(self.project_root)

        if strategy == NONE:
            # In a git repo we diff against the base commit; otherwise capture an immutable baseline
            # snapshot so an in-place run's before/after are never read from the same folder (item 5).
            baseline_dir = None if git else self._make_baseline(run_id)
            return Workspace(
                run_id=run_id,
                root=self.project_root,
                source=self.project_root,
                is_git=git,
                strategy=NONE,
                in_place=True,
                baseline_dir=baseline_dir,
            )

        if strategy == AUTO and git:
            return self._git_worktree(run_id)

        # strategy == COPY, or AUTO on a non-git project → isolated copy (lower safety).
        return self._copy_workspace(run_id, git)

    def _git_worktree(self, run_id: str) -> Workspace:
        base_commit = _git(["rev-parse", "HEAD"], self.project_root).strip()
        branch = f"openagent/{run_id}"
        target = self.worktrees_dir / run_id
        GIT.mutate_worktree(
            ["worktree", "add", "-b", branch, str(target), base_commit], self.project_root
        )
        workspace = Workspace(
            run_id=run_id,
            root=target,
            source=self.project_root,
            is_git=True,
            strategy=AUTO,
            branch=branch,
            base_commit=base_commit,
        )
        self._record_ownership(workspace)
        return workspace

    def _copy_workspace(self, run_id: str, git: bool) -> Workspace:
        target = self.worktrees_dir / run_id
        if target.exists():
            safe_rmtree(target, owner_root=self.worktrees_dir)
        target.mkdir(parents=True)
        SafeWorkspaceWalker(self.project_root).copy_to(target, ignore_dirs=_IGNORE_DIRS)
        # The working copy is the "after"; an immutable baseline snapshot is the "before".
        workspace = Workspace(
            run_id=run_id,
            root=target,
            source=self.project_root,
            is_git=git,
            strategy=COPY,
            is_copy=True,
            baseline_dir=self._make_baseline(run_id),
        )
        self._record_ownership(workspace)
        return workspace

    def _make_baseline(self, run_id: str) -> Path:
        """Copy the untouched source tree into an immutable baseline snapshot dir (item 5)."""

        baseline = self.worktrees_dir / f"{run_id}__baseline"
        if baseline.exists():
            safe_rmtree(baseline, owner_root=self.worktrees_dir)
        baseline.mkdir(parents=True)
        SafeWorkspaceWalker(self.project_root).copy_to(baseline, ignore_dirs=_IGNORE_DIRS)
        return baseline

    def _owner_marker(self, run_id: str) -> Path:
        return self.worktrees_dir / f".{run_id}.owner.json"

    def _record_ownership(self, ws: Workspace) -> None:
        marker = {
            "owner": "openagent",
            "run_id": ws.run_id,
            "root": str(ws.root.absolute()),
            "branch": ws.branch,
            "strategy": ws.strategy,
        }
        atomic_write_text(self._owner_marker(ws.run_id), json.dumps(marker), mode=0o600)

    def _verify_ownership(self, ws: Workspace) -> Path:
        marker_path = self._owner_marker(ws.run_id)
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GitError(f"ownership metadata missing for {ws.root}") from exc
        expected = {
            "owner": "openagent",
            "run_id": ws.run_id,
            "root": str(ws.root.absolute()),
            "branch": ws.branch,
            "strategy": ws.strategy,
        }
        if marker != expected:
            raise GitError(f"ownership metadata does not match {ws.root}")
        if ws.branch and not ws.branch.startswith("openagent/"):
            raise GitError(f"refused cleanup of non-OpenAgent branch {ws.branch}")
        return marker_path

    # ------------------------------------------------------------------ inspection

    def changed_files(self, ws: Workspace) -> list[str]:
        if self._uses_git_diff(ws):
            return sorted(_porcelain_paths(_git(["status", "--porcelain=v1", "-z"], ws.root)))
        return sorted(self._compare(ws)[0])

    def diff(self, ws: Workspace) -> str:
        """Combined diff of the run's changes — **without touching the user's index** (spec §10).

        This used to run ``git add -A -N`` to make untracked files visible to ``git diff``. That
        writes intent-to-add entries into the real index, and for an in-place run (``--worktree
        none``) ``ws.root`` *is* the user's own repository — so producing a diff silently staged
        their untracked files. Reading state must not mutate it.

        Instead: ``git diff`` for tracked changes, plus a ``--no-index`` diff per untracked file
        (which compares two paths directly and never consults, or writes, the index).
        """

        if not self._uses_git_diff(ws):
            return self._text_diff(ws)
        # GIT.diff, not _git(["diff"]): it adds --no-ext-diff/--no-textconv. A textconv filter is
        # bound through the repository's own .gitattributes, so clearing `diff.external` in config
        # does not reach it — the flags are the only thing that does.
        parts = [GIT.diff([], ws.root)]
        for rel in _untracked_files(ws.root):
            parts.append(_untracked_diff(ws.root, rel))
        return "".join(p for p in parts if p)

    def _uses_git_diff(self, ws: Workspace) -> bool:
        # Git diff works for a real worktree, and for an in-place run inside a git repo.
        return ws.is_git and not ws.is_copy

    # ------------------------------------------------------------------ copy diffing

    def _snapshot(self, root: Path) -> dict[str, str]:
        """Map workspace-relative path → content digest for every text/binary file under ``root``."""

        import hashlib

        snap: dict[str, str] = {}
        walker = SafeWorkspaceWalker(root)
        for path in walker.iter_files(ignore_dirs=_IGNORE_DIRS):
            try:
                relative = path.relative_to(root)
                snap[str(relative)] = hashlib.sha1(walker.read_bytes(relative)).hexdigest()
            except (OSError, UnsafeWorkspacePath):  # pragma: no cover - unreadable file
                continue
        return snap

    def _compare(self, ws: Workspace) -> tuple[list[str], list[str], list[str]]:
        """Return (changed, created, deleted) relative paths for a copy/in-place run.

        Compares the workspace against the **immutable baseline snapshot** (never the live source),
        so this is correct on the first run and on every resumed turn (item 5).
        """

        after = self._snapshot(ws.root)
        before = self._snapshot(ws.baseline_dir) if ws.baseline_dir else {}
        created = [p for p in after if p not in before]
        deleted = [p for p in before if p not in after]
        modified = [p for p in after if p in before and after[p] != before[p]]
        changed = sorted({*created, *deleted, *modified})
        return changed, sorted(created), sorted(deleted)

    def _text_diff(self, ws: Workspace) -> str:
        changed, created, deleted = self._compare(ws)
        # "before" always comes from the immutable baseline snapshot, "after" from the workspace —
        # never the same folder, so an in-place non-git run produces a real diff (item 5).
        before_root = ws.baseline_dir or ws.source
        chunks: list[str] = []
        for rel in changed:
            before = _read_text(before_root, rel) if rel not in created else ""
            after = _read_text(ws.root, rel) if rel not in deleted else ""
            if before is None or after is None:  # binary; note the change without a body
                chunks.append(f"Binary file {rel} changed\n")
                continue
            diff = difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
            chunks.append("".join(diff))
        return ("".join(chunks))[:_MAX_DIFF_BYTES]

    # ------------------------------------------------------------------ disposition

    def discard(self, ws: Workspace) -> None:
        """Remove the worktree/copy and its branch (spec §28). No-op for in-place runs.

        The immutable baseline snapshot (if any) is always cleaned up too.
        """

        if ws.in_place:
            if ws.baseline_dir and ws.baseline_dir.exists():
                safe_rmtree(ws.baseline_dir, owner_root=self.worktrees_dir)
            return
        marker = self._verify_ownership(ws)
        if ws.baseline_dir and ws.baseline_dir.exists():
            safe_rmtree(ws.baseline_dir, owner_root=self.worktrees_dir)
        if ws.is_copy:
            if ws.root.exists():
                safe_rmtree(ws.root, owner_root=self.worktrees_dir)
            marker.unlink(missing_ok=True)
            return
        try:
            GIT.mutate_worktree(["worktree", "remove", "--force", str(ws.root)], self.project_root)
        except GitError:
            if ws.root.exists():
                safe_rmtree(ws.root, owner_root=self.worktrees_dir)
        if ws.branch:
            try:
                GIT.mutate_worktree(["branch", "-D", ws.branch], self.project_root)
            except GitError:
                pass
        marker.unlink(missing_ok=True)

    def commit_all(self, ws: Workspace, message: str) -> str | None:
        """Commit everything in the worktree; returns the new commit sha (git worktree only)."""

        if not ws.is_git or ws.is_copy or ws.in_place:
            return None
        self._verify_ownership(ws)
        GIT.mutate_worktree(["add", "-A"], ws.root)
        status = _git(["status", "--porcelain"], ws.root)
        if not status.strip():
            return None
        # Pinned identity, no signing, no hooks. Previously a plain `git commit`, which ran the
        # repository's commit-lifecycle hooks with the parent environment attached.
        GIT.commit_agent_changes(message, ws.root)
        return _git(["rev-parse", "HEAD"], ws.root).strip()

    def revert_commit(self, ws: Workspace, commit_sha: str) -> str:
        if not ws.is_git or ws.is_copy or ws.in_place:
            raise GitError("agent commit can only be reverted in its OpenAgent git worktree")
        self._verify_ownership(ws)
        actual = _git(["rev-parse", commit_sha], ws.root).strip()
        if actual != commit_sha:
            raise GitError("recorded agent commit does not resolve exactly")
        GIT.mutate_worktree(["revert", "--no-edit", commit_sha], ws.root)
        return _git(["rev-parse", "HEAD"], ws.root).strip()


def _read_text(root: Path, relative: str) -> str | None:
    try:
        return SafeWorkspaceWalker(root).read_bytes(relative).decode("utf-8")
    except (OSError, UnicodeDecodeError, UnsafeWorkspacePath):
        return None
