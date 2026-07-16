"""Copy / non-git resume diffing keeps a correct baseline across turns (item 5).

Regression coverage for the bug where a resumed copy/non-git run lost its baseline and reported every
file as newly "created". Uses the real fake-CLI subprocess and the real worktree diffing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import RunStatus, RuntimeType
from tests.fakecli import FakeCliAdapter, install_fake_cli, write_fake_script


def _project(tmp_path: Path, *, git: bool) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "seed.txt").write_text("original line\n")
    (project / "todelete.txt").write_text("delete me\n")
    if git:

        def g(*a):
            subprocess.run(["git", *a], cwd=str(project), check=True, capture_output=True)

        g("init", "-q")
        g("config", "user.email", "t@t.com")
        g("config", "user.name", "t")
        g("add", "-A")
        g("commit", "-q", "-m", "init")
    return project


def _oa(tmp_path: Path, project: Path) -> OpenAgentApp:
    paths = Paths(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db",
        project_root=project,
    )
    oa = OpenAgentApp(paths)
    oa.agents.create(name="fake-coder", runtime_type=RuntimeType.CLI, cli="fake")
    return oa


def _wire(monkeypatch, tmp_path, mode, resume_mode):
    adapter = FakeCliAdapter(write_fake_script(tmp_path), mode=mode, resume_mode=resume_mode)
    install_fake_cli(monkeypatch, adapter)


@pytest.mark.parametrize("git", [False, True])
async def test_copy_run_and_resume_diff(tmp_path: Path, monkeypatch, git: bool):
    project = _project(tmp_path, git=git)
    oa = _oa(tmp_path, project)
    _wire(monkeypatch, tmp_path, "mutate", "mutate2")

    run = oa.runs.create(agent_name="fake-coder", prompt="turn 1", worktree="copy")
    result = await oa.runs.execute(run)
    assert result.status == RunStatus.COMPLETED

    changed = set(result.files_changed)
    assert "created1.txt" in changed  # created
    assert "seed.txt" in changed  # modified
    assert "todelete.txt" in changed  # deleted
    diff1 = oa.runs.output(run.id, "diff")
    assert "created in turn1" in diff1
    assert "turn1 append" in diff1

    resumed = await oa.runs.resume(run.id, "turn 2")
    assert resumed.status == RunStatus.COMPLETED
    changed2 = set(resumed.files_changed)
    # Both turns' work is present; nothing was wrongly dropped or all-marked-created.
    assert {"created1.txt", "created2.txt", "seed.txt", "todelete.txt"} <= changed2
    diff2 = oa.runs.output(run.id, "diff")
    assert "created in turn1" in diff2 and "created in turn2" in diff2
    assert "turn1 append" in diff2 and "turn2 append" in diff2
    # seed.txt is a modification, not a creation: its original line is in the diff's "before" side.
    assert "original line" in diff2


async def test_failed_resume_preserves_prior_copy_diff(tmp_path: Path, monkeypatch):
    project = _project(tmp_path, git=False)
    oa = _oa(tmp_path, project)
    _wire(monkeypatch, tmp_path, "mutate", "fail1")

    run = oa.runs.create(agent_name="fake-coder", prompt="turn 1", worktree="copy")
    await oa.runs.execute(run)

    resumed = await oa.runs.resume(run.id, "make it fail")
    assert resumed.status == RunStatus.FAILED
    # The earlier turn's changes are not erased by the failed resume.
    changed = set(resumed.files_changed)
    assert {"created1.txt", "seed.txt", "todelete.txt"} <= changed
    diff = oa.runs.output(run.id, "diff")
    assert "created in turn1" in diff


async def test_non_git_in_place_diff_reads_distinct_baseline(tmp_path: Path, monkeypatch):
    # 'none' on a non-git project must diff against the immutable snapshot, not the live folder.
    project = _project(tmp_path, git=False)
    oa = _oa(tmp_path, project)
    _wire(monkeypatch, tmp_path, "mutate", "mutate2")

    run = oa.runs.create(
        agent_name="fake-coder", prompt="go", worktree="none", confirm_in_place=True
    )
    result = await oa.runs.execute(run)
    assert result.status == RunStatus.COMPLETED
    diff = oa.runs.output(run.id, "diff")
    # A real diff was produced even though before/after live in the same project dir.
    assert "created in turn1" in diff
    assert "turn1 append" in diff
    assert "created1.txt" in set(result.files_changed)
