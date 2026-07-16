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
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

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
_MAX_DIFF_BYTES = 400_000

AUTO = "auto"
NONE = "none"
COPY = "copy"
STRATEGIES = (AUTO, NONE, COPY)


class GitError(RuntimeError):
    pass


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def is_git_repo(path: Path) -> bool:
    try:
        out = _git(["rev-parse", "--is-inside-work-tree"], path)
        return out.strip() == "true"
    except GitError:
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
        _git(["worktree", "add", "-b", branch, str(target), base_commit], self.project_root)
        return Workspace(
            run_id=run_id,
            root=target,
            source=self.project_root,
            is_git=True,
            strategy=AUTO,
            branch=branch,
            base_commit=base_commit,
        )

    def _copy_workspace(self, run_id: str, git: bool) -> Workspace:
        target = self.worktrees_dir / run_id
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(
            self.project_root,
            target,
            ignore=shutil.ignore_patterns(*_IGNORE_DIRS),
        )
        # The working copy is the "after"; an immutable baseline snapshot is the "before".
        return Workspace(
            run_id=run_id,
            root=target,
            source=self.project_root,
            is_git=git,
            strategy=COPY,
            is_copy=True,
            baseline_dir=self._make_baseline(run_id),
        )

    def _make_baseline(self, run_id: str) -> Path:
        """Copy the untouched source tree into an immutable baseline snapshot dir (item 5)."""

        baseline = self.worktrees_dir / f"{run_id}__baseline"
        if baseline.exists():
            shutil.rmtree(baseline)
        shutil.copytree(
            self.project_root,
            baseline,
            ignore=shutil.ignore_patterns(*_IGNORE_DIRS),
        )
        return baseline

    # ------------------------------------------------------------------ inspection

    def changed_files(self, ws: Workspace) -> list[str]:
        if self._uses_git_diff(ws):
            out = _git(["status", "--porcelain"], ws.root)
            files = []
            for line in out.splitlines():
                if line.strip():
                    files.append(line[3:].strip())
            return sorted(files)
        return sorted(self._compare(ws)[0])

    def diff(self, ws: Workspace) -> str:
        """Combined diff of the run's changes."""

        if self._uses_git_diff(ws):
            _git(["add", "-A", "-N"], ws.root)  # intent-to-add so new files show in diff
            return _git(["diff"], ws.root)
        return self._text_diff(ws)

    def _uses_git_diff(self, ws: Workspace) -> bool:
        # Git diff works for a real worktree, and for an in-place run inside a git repo.
        return ws.is_git and not ws.is_copy

    # ------------------------------------------------------------------ copy diffing

    def _snapshot(self, root: Path) -> dict[str, str]:
        """Map workspace-relative path → content digest for every text/binary file under ``root``."""

        import hashlib

        snap: dict[str, str] = {}
        for path in self._walk(root):
            try:
                snap[str(path.relative_to(root))] = hashlib.sha1(path.read_bytes()).hexdigest()
            except OSError:  # pragma: no cover - unreadable file
                continue
        return snap

    def _walk(self, root: Path):
        import os

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
            for name in filenames:
                yield Path(dirpath) / name

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
            src = before_root / rel
            dst = ws.root / rel
            before = _read_text(src) if rel not in created else ""
            after = _read_text(dst) if rel not in deleted else ""
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

        if ws.baseline_dir and ws.baseline_dir.exists():
            shutil.rmtree(ws.baseline_dir, ignore_errors=True)
        if ws.in_place:
            return
        if ws.is_copy:
            if ws.root.exists():
                shutil.rmtree(ws.root, ignore_errors=True)
            return
        try:
            _git(["worktree", "remove", "--force", str(ws.root)], self.project_root)
        except GitError:
            if ws.root.exists():
                shutil.rmtree(ws.root, ignore_errors=True)
        if ws.branch:
            try:
                _git(["branch", "-D", ws.branch], self.project_root)
            except GitError:
                pass

    def commit_all(self, ws: Workspace, message: str) -> str | None:
        """Commit everything in the worktree; returns the new commit sha (git worktree only)."""

        if not ws.is_git or ws.is_copy or ws.in_place:
            return None
        _git(["add", "-A"], ws.root)
        status = _git(["status", "--porcelain"], ws.root)
        if not status.strip():
            return None
        _git(["commit", "-m", message], ws.root)
        return _git(["rev-parse", "HEAD"], ws.root).strip()


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
