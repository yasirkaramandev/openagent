"""Git handling is bounded, honest, and read-only where it claims to be (spec §10).

Four independent defects:

1. **The user's index was mutated to produce a diff.** ``diff()`` ran ``git add -A -N`` so untracked
   files would appear. For an in-place run (``--worktree none``) ``ws.root`` *is* the user's own
   repository, so merely *looking* at the diff silently staged their untracked files. Reading state
   must not change it.
2. **No timeouts.** Every git call was an unbounded ``subprocess.run``. git blocks indefinitely on a
   held ``index.lock``, a credential prompt, or a slow remote — hanging the run with no diagnosis.
3. **A missing git crashed setup.** ``is_git_repo()`` caught only ``GitError``, so
   ``FileNotFoundError`` from ``subprocess.run(["git", ...])`` propagated instead of degrading to a
   copy workspace.
4. **The filename parser was wrong.** ``git status --porcelain`` + ``line[3:]`` mangles quoted names
   (``"a\\tb"``), renames (``R old -> new``), and anything with a newline.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from openagent.workspaces.worktree import (
    GitMissing,
    GitTimeout,
    Workspace,
    WorktreeManager,
    _porcelain_paths,
    is_git_repo,
)


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@t.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "seed.txt").write_text("seed\n")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "init"], root)
    return root


def _in_place(root: Path) -> Workspace:
    return Workspace(
        run_id="r",
        root=root,
        source=root,
        is_git=True,
        strategy="none",
        is_copy=False,
        in_place=True,
    )


def _manager(root: Path) -> WorktreeManager:
    return WorktreeManager(root, root / ".openagent" / "worktrees")


# --------------------------------------------------------------------------- §10 the user's index


def test_diff_does_not_touch_the_users_index(repo: Path):
    """The headline: producing a diff must leave `git diff --cached` byte-identical."""

    (repo / "untracked.txt").write_text("new file\n")
    (repo / "seed.txt").write_text("seed modified\n")
    before = _git(["diff", "--cached"], repo)
    before_status = _git(["status", "--porcelain=v1", "-z"], repo)

    _manager(repo).diff(_in_place(repo))

    assert _git(["diff", "--cached"], repo) == before, (
        "diff() staged the user's files — the index is not byte-equivalent"
    )
    assert _git(["status", "--porcelain=v1", "-z"], repo) == before_status


def test_diff_still_includes_untracked_files(repo: Path):
    """Not mutating the index must not cost us the information it was used to get."""

    (repo / "untracked.txt").write_text("brand new content here\n")
    diff = _manager(repo).diff(_in_place(repo))
    assert "untracked.txt" in diff
    assert "brand new content here" in diff


def test_diff_includes_tracked_modifications(repo: Path):
    (repo / "seed.txt").write_text("seed modified\n")
    diff = _manager(repo).diff(_in_place(repo))
    assert "seed.txt" in diff and "seed modified" in diff


def test_diff_ignores_gitignored_files(repo: Path):
    (repo / ".gitignore").write_text("secret.txt\n")
    (repo / "secret.txt").write_text("should not appear\n")
    diff = _manager(repo).diff(_in_place(repo))
    assert "should not appear" not in diff


def test_index_survives_a_staged_change(repo: Path):
    """A user who had something staged must still have exactly that staged afterwards."""

    (repo / "staged.txt").write_text("i was staged\n")
    _git(["add", "staged.txt"], repo)
    before = _git(["diff", "--cached"], repo)

    _manager(repo).diff(_in_place(repo))

    assert _git(["diff", "--cached"], repo) == before


# --------------------------------------------------------------------------- §10 missing git


def test_is_git_repo_is_false_when_git_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A machine without git must still run agents — `auto` degrades to a copy."""

    def no_git(*_a, **_k):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr(subprocess, "run", no_git)
    assert is_git_repo(tmp_path) is False


def test_auto_strategy_falls_back_to_a_copy_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.txt").write_text("x")

    real_run = subprocess.run

    def no_git(args, *a, **k):
        if args and args[0] == "git":
            raise FileNotFoundError(2, "No such file or directory: 'git'")
        return real_run(args, *a, **k)

    monkeypatch.setattr(subprocess, "run", no_git)
    ws = WorktreeManager(project, tmp_path / "wt").create("run_1", strategy="auto")
    assert ws.is_copy is True and ws.is_git is False
    assert ws.lower_safety is True, "a copy is lower safety and must be reported as such"


def test_git_missing_is_a_typed_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A machine without git raises GitMissing, not a bare FileNotFoundError.

    Patched at ``git_runner.run_capture`` rather than at ``subprocess.run``: git execution moved
    into ``security/git_runner`` in v0.1.5 so that hooks and the parent environment could be
    stripped in one place. The invariant under test is unchanged — only where the boundary sits.
    """

    from openagent.security import git_runner

    def no_git(*_a, **_k):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr(git_runner, "run_capture", no_git)
    with pytest.raises(GitMissing):
        git_runner.GIT.inspect(["status"], tmp_path)


# --------------------------------------------------------------------------- §10 timeouts


def test_git_timeout_is_typed_and_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from openagent.security import git_runner

    def hang(*_a, **kwargs):
        assert kwargs.get("timeout"), "git was invoked with no timeout — it can hang forever"
        raise subprocess.TimeoutExpired(cmd="git", timeout=kwargs["timeout"])

    monkeypatch.setattr(git_runner, "run_capture", hang)
    with pytest.raises(GitTimeout):
        git_runner.GIT.inspect(["status"], tmp_path)


def test_every_git_call_passes_a_timeout(repo: Path, monkeypatch: pytest.MonkeyPatch):
    """No git invocation reaches the OS without a timeout, on either the read or the diff path."""

    from openagent.security import git_runner

    seen: list[object] = []
    real_capture = git_runner.run_capture

    def record(argv, *a, **kwargs):
        if argv and argv[0] == "git":
            seen.append(kwargs.get("timeout"))
        return real_capture(argv, *a, **kwargs)

    monkeypatch.setattr(git_runner, "run_capture", record)
    _manager(repo).changed_files(_in_place(repo))
    _manager(repo).diff(_in_place(repo))
    assert seen, "no git calls were observed"
    assert all(t for t in seen), f"a git call had no timeout: {seen}"


def test_no_git_call_bypasses_the_hardened_runner(repo: Path, monkeypatch: pytest.MonkeyPatch):
    """Every git process the workspace layer starts goes through the isolating runner.

    A second, separately written ``subprocess.run(["git", ...])`` is exactly how the untracked-file
    diff path kept inheriting ``os.environ`` after the main path had been fixed. This fails if any
    such call is reintroduced anywhere in the workspace layer.
    """

    escaped: list[list[str]] = []
    real_run = subprocess.run

    def record(args, *a, **kwargs):
        if isinstance(args, (list, tuple)) and args and args[0] == "git":
            escaped.append(list(args))
        return real_run(args, *a, **kwargs)

    monkeypatch.setattr(subprocess, "run", record)
    (repo / "untracked.txt").write_text("new\n", encoding="utf-8")
    _manager(repo).changed_files(_in_place(repo))
    _manager(repo).diff(_in_place(repo))

    assert escaped == [], f"git was invoked outside GitRunner: {escaped}"


# --------------------------------------------------------------------------- §10 filename parsing


def test_porcelain_parser_handles_awkward_names():
    """`-z` output: `XY <path>\\0`, with a rename adding an origin record."""

    raw = (
        "\0".join(
            [
                " M simple.txt",
                "?? with space.txt",
                "?? with\ttab.txt",
                '?? quote".txt',
                "?? -leading-dash.txt",
                "?? ünïcode.txt",
                "R  new_name.txt",
                "old_name.txt",  # the rename's ORIGIN record
                " M after_rename.txt",
            ]
        )
        + "\0"
    )
    paths = _porcelain_paths(raw)
    assert "simple.txt" in paths
    assert "with space.txt" in paths
    assert "with\ttab.txt" in paths
    assert 'quote".txt' in paths
    assert "-leading-dash.txt" in paths
    assert "ünïcode.txt" in paths
    assert "new_name.txt" in paths
    # The origin record must be consumed, not misread as its own entry…
    assert "old_name.txt" not in paths
    # …and the entry after a rename must still parse.
    assert "after_rename.txt" in paths


@pytest.mark.parametrize(
    "name",
    [
        "with space.txt",
        "with'quote.txt",
        "ünïcode-ファイル.txt",
        "-leading-dash.txt",
        "trailing.space .txt",
    ],
)
def test_changed_files_reports_awkward_real_filenames(repo: Path, name: str):
    (repo / name).write_text("content\n")
    files = _manager(repo).changed_files(_in_place(repo))
    assert name in files, f"{name!r} was not reported correctly"


def test_changed_files_reports_a_real_rename(repo: Path):
    (repo / "before.txt").write_text("some content to rename\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "add"], repo)
    _git(["mv", "before.txt", "after.txt"], repo)

    files = _manager(repo).changed_files(_in_place(repo))
    assert "after.txt" in files
    # And the index must be exactly what `git mv` left — diffing must not have added to it.
    before = _git(["diff", "--cached"], repo)
    _manager(repo).diff(_in_place(repo))
    assert _git(["diff", "--cached"], repo) == before
