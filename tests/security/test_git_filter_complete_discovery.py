"""Effective Git filter config and nested attributes must be neutralised completely."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from openagent.security.git_runner import GIT

pytestmark = [pytest.mark.security, pytest.mark.subprocess]


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
@pytest.mark.skipif(os.name != "posix", reason="payload uses a POSIX shell script")
def test_nested_attribute_and_included_filter_definition_never_execute(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "seed")

    marker = tmp_path / "filter-executed"
    script = tmp_path / "filter-payload.sh"
    script.write_text(
        f"#!/bin/sh\nprintf executed > {str(marker)!r}\ncat\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    included = repo / ".git" / "openagent-filter.inc"
    included.write_text(
        f'[filter "nested.with.dot"]\n\tclean = {script}\n\tsmudge = {script}\n\trequired = true\n',
        encoding="utf-8",
    )
    _git(repo, "config", "--local", "include.path", str(included))

    nested = repo / "nested"
    nested.mkdir()
    (nested / ".gitattributes").write_text("*.txt filter=nested.with.dot\n", encoding="utf-8")
    (nested / "payload.txt").write_text("payload\n", encoding="utf-8")

    result = GIT.mutate_worktree(["add", "nested/payload.txt"], repo)

    assert result.returncode == 0
    assert not marker.exists(), "repository-controlled content filter payload executed"
