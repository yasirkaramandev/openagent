"""Fail-closed coverage for ambiguous Git filter and attribute discovery."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from openagent.security import git_runner
from openagent.security.git_runner import (
    GitMissing,
    GitTimeout,
    UnsafeGitFilterConfiguration,
)

pytestmark = pytest.mark.security


def test_config_query_classifies_absent_empty_and_failed_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        git_runner, "run_capture", lambda *_a, **_kw: SimpleNamespace(returncode=1, stdout="")
    )
    assert git_runner._config_query(tmp_path, ["--get", "missing"]) == []

    monkeypatch.setattr(
        git_runner, "run_capture", lambda *_a, **_kw: SimpleNamespace(returncode=2, stdout="")
    )
    with pytest.raises(UnsafeGitFilterConfiguration):
        git_runner._config_query(tmp_path, ["--get", "broken"])

    def missing(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(git_runner, "run_capture", missing)
    with pytest.raises(GitMissing):
        git_runner._config_query(tmp_path, [])

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["git"], 1)

    monkeypatch.setattr(git_runner, "run_capture", timeout)
    with pytest.raises(GitTimeout):
        git_runner._config_query(tmp_path, [])

    def os_error(*_args, **_kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr(git_runner, "run_capture", os_error)
    with pytest.raises(UnsafeGitFilterConfiguration):
        git_runner._config_query(tmp_path, [])


def test_ambiguous_filter_names_and_attribute_syntax_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    monkeypatch.setattr(
        git_runner,
        "_config_query",
        lambda _cwd, args: ["filter.bad name.clean"] if "--name-only" in args else [],
    )
    with pytest.raises(UnsafeGitFilterConfiguration):
        git_runner._discover_filter_names(repo)

    monkeypatch.setattr(git_runner, "_config_query", lambda *_a, **_kw: [])
    (repo / ".gitattributes").write_text("# ignored\n*.txt filter=\n", encoding="utf-8")
    with pytest.raises(UnsafeGitFilterConfiguration):
        git_runner._discover_filter_names(repo)

    (repo / ".gitattributes").write_text("*.txt filter=bad:name\n", encoding="utf-8")
    with pytest.raises(UnsafeGitFilterConfiguration):
        git_runner._discover_filter_names(repo)


def test_attribute_discovery_rejects_links_limits_and_external_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    target = repo / "real-attributes"
    target.write_text("*.txt filter=safe\n", encoding="utf-8")
    (repo / ".gitattributes").symlink_to(target)
    with pytest.raises(UnsafeGitFilterConfiguration):
        git_runner._attribute_sources(repo)

    (repo / ".gitattributes").unlink()
    (repo / ".gitattributes").write_text("*.txt filter=safe\n", encoding="utf-8")
    monkeypatch.setattr(git_runner, "_ATTR_FILE_LIMIT", 0)
    with pytest.raises(UnsafeGitFilterConfiguration):
        git_runner._attribute_sources(repo)

    monkeypatch.setattr(git_runner, "_ATTR_FILE_LIMIT", 256)
    outside = tmp_path / "outside"
    outside.write_text("*.txt filter=safe\n", encoding="utf-8")
    monkeypatch.setattr(
        git_runner,
        "_config_query",
        lambda _cwd, args: [str(outside)] if "core.attributesFile" in args else [],
    )
    with pytest.raises(UnsafeGitFilterConfiguration):
        git_runner._attribute_sources(repo)

    linked = repo / "linked-attributes"
    linked.symlink_to(repo / ".gitattributes")
    monkeypatch.setattr(
        git_runner,
        "_config_query",
        lambda _cwd, args: [str(linked)] if "core.attributesFile" in args else [],
    )
    with pytest.raises(UnsafeGitFilterConfiguration):
        git_runner._attribute_sources(repo)


def test_linked_git_directory_and_attribute_decode_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    gitdir = tmp_path / "git-dir"
    common = tmp_path / "common"
    (gitdir / "info").mkdir(parents=True)
    common.mkdir()
    (gitdir / "commondir").write_text(str(common), encoding="utf-8")
    (repo / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    assert git_runner._git_dirs(repo) == [gitdir.resolve(), common.resolve()]

    attributes = gitdir / "info" / "attributes"
    attributes.write_bytes(b"\xff")
    monkeypatch.setattr(git_runner, "_config_query", lambda *_a, **_kw: [])
    with pytest.raises(UnsafeGitFilterConfiguration):
        git_runner._discover_filter_names(repo)
