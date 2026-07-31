"""Checkout-independent, channel-based self-update (spec §5-§19).

These tests cover the behavior that fixes the reported bug: ``openagent update`` must work from any
directory, on any branch, with a dirty or missing checkout, by installing an exact official commit
for the resolved channel — never by inspecting a local Git checkout's branch/cleanliness.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from openagent.runtimes.cli.locator import CommandResult
from openagent.services.self_update import (
    CANDIDATE_BRANCH,
    OFFICIAL_HTTPS_REMOTE,
    OFFICIAL_REPOSITORY,
    InstallMetadata,
    OpenAgentUpdateChannel,
    SelfUpdateTarget,
    _infer_channel,
    _vcs_provenance,
    check_self_update,
    install_metadata_path,
    read_install_metadata,
    resolve_vcs_target,
    write_install_metadata,
)

A = "a" * 40
B = "b" * 40
C = "c" * 40


def _active(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("entrypoint", encoding="utf-8")
    path.chmod(0o755)
    return path


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


def _vcs_direct_url(commit: str, *, revision: str = CANDIDATE_BRANCH) -> dict:
    return {
        "url": OFFICIAL_HTTPS_REMOTE,
        "vcs_info": {"vcs": "git", "requested_revision": revision, "commit_id": commit},
    }


def _file_direct_url(root: Path) -> dict:
    return {"url": root.as_uri(), "dir_info": {}}


def _target(
    commit: str,
    *,
    version: str = "0.1.6rc4",
    channel: OpenAgentUpdateChannel = OpenAgentUpdateChannel.CANDIDATE,
    ref: str | None = CANDIDATE_BRANCH,
) -> SelfUpdateTarget:
    return SelfUpdateTarget(
        channel=channel,
        repository=OFFICIAL_REPOSITORY,
        ref=ref,
        commit_sha=commit,
        version=version,
        package_url=f"git+{OFFICIAL_HTTPS_REMOTE}@{commit}",
        source_kind="github-vcs",
        ci_verified=channel is OpenAgentUpdateChannel.CANDIDATE,
        prerelease=True,
    )


@pytest.fixture
def fake_uv(monkeypatch, tmp_path):
    uv = _active(tmp_path / "uvbin" / "uv")
    monkeypatch.setattr(
        "openagent.services.self_update.shutil.which",
        lambda name, path=None: str(uv) if name == "uv" else None,
    )
    return str(uv)


# ------------------------------------------------------------------ reproduction of the bug (spec §4)


def _detached_or_feature_branch_runner(root: Path):
    """A git runner modeling the user's real checkout: a legacy file:// install on a non-main branch.

    ``branch --show-current`` returns empty (detached HEAD) — exactly the ``openagent-rc4`` worktree
    the user's binary was installed from.
    """

    def runner(argv, timeout, limit, env, cwd):
        del timeout, limit, env, cwd
        tail = list(argv[3:]) if argv[:1] == ["git"] and argv[1] == "-C" else list(argv[1:])
        if tail[:1] == ["rev-parse"] and "--show-toplevel" in tail:
            return _result(stdout=f"{root}\n")
        if tail[:2] == ["remote", "get-url"]:
            return _result(stdout="git@github.com:yasirkaramandev/openagent.git\n")
        if tail[:1] == ["branch"]:
            return _result(stdout="\n")  # detached HEAD → empty current branch
        return _result(stdout="")

    return runner


def test_reproduces_and_fixes_checkout_bound_failure(tmp_path, fake_uv) -> None:
    """The reported failure and its fix, side by side.

    OLD (opt-in developer path): a legacy file:// install on a detached HEAD is blocked with
    "must be on branch main". NEW (default): the same install migrates to the official candidate
    channel and installs an exact commit — no checkout branch/cleanliness is ever consulted.
    """

    root = tmp_path / "openagent-rc4"
    root.mkdir()
    (root / "setup.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    active = _active(tmp_path / "tool" / "openagent")

    # OLD behavior is still reachable, and still reproduces the exact error, under local_dev=True.
    blocked = check_self_update(
        current_version="0.1.6rc4",
        active_executable=str(active),
        direct_url=_file_direct_url(root),
        metadata=None,
        runner=_detached_or_feature_branch_runner(root),
        platform="linux",
        local_dev=True,
    )
    assert blocked.can_update is False
    assert "must be on branch main" in blocked.detail

    # NEW default behavior: migrate to the candidate channel, checkout-independently.
    plan = check_self_update(
        current_version="0.1.6rc4",
        active_executable=str(active),
        direct_url=_file_direct_url(root),
        metadata=None,
        target_resolver=lambda ch: _target(B),
        platform="linux",
    )
    assert plan.can_update is True
    assert plan.update_available is True
    assert plan.migrating is True
    assert plan.channel is OpenAgentUpdateChannel.CANDIDATE
    assert plan.reason == "migrate"
    assert plan.source == "official-github-vcs"
    assert plan.commands == [
        [
            fake_uv,
            "tool",
            "install",
            "--force",
            "--reinstall",
            "--refresh",
            "--python",
            "3.12",
            f"git+{OFFICIAL_HTTPS_REMOTE}@{B}",
        ]
    ]
    assert "must be on branch main" not in plan.detail


# ---------------------------------------------------------------------------- VCS provenance (spec §9)


def test_vcs_provenance_accepts_official_https_commit() -> None:
    prov = _vcs_provenance(_vcs_direct_url(A))
    assert prov is not None
    assert prov.repository == OFFICIAL_REPOSITORY
    assert prov.commit_id == A
    assert prov.url == OFFICIAL_HTTPS_REMOTE


def test_vcs_provenance_accepts_ssh_official_remote() -> None:
    prov = _vcs_provenance(
        {
            "url": "git@github.com:yasirkaramandev/openagent.git",
            "vcs_info": {"vcs": "git", "commit_id": B},
        }
    )
    assert prov is not None and prov.commit_id == B


@pytest.mark.parametrize(
    "payload",
    [
        {
            "url": "https://token@github.com/yasirkaramandev/openagent.git",
            "vcs_info": {"vcs": "git", "commit_id": A},
        },  # credentials in URL
        {
            "url": "http://github.com/yasirkaramandev/openagent.git",
            "vcs_info": {"vcs": "git", "commit_id": A},
        },  # unencrypted
        {
            "url": "https://github.com/attacker/openagent.git",
            "vcs_info": {"vcs": "git", "commit_id": A},
        },  # wrong repo
        {"url": OFFICIAL_HTTPS_REMOTE, "vcs_info": {"vcs": "hg", "commit_id": A}},  # not git
        {
            "url": OFFICIAL_HTTPS_REMOTE,
            "vcs_info": {"vcs": "git", "commit_id": "z" * 40},
        },  # not hex
        {
            "url": OFFICIAL_HTTPS_REMOTE,
            "vcs_info": {"vcs": "git", "commit_id": "abc123"},
        },  # short sha
        {"url": OFFICIAL_HTTPS_REMOTE, "vcs_info": {"vcs": "git"}},  # no commit
    ],
)
def test_vcs_provenance_rejects_untrusted(payload) -> None:
    assert _vcs_provenance(payload) is None


# ---------------------------------------------------------------------- channel inference (spec §6.4)


def test_channel_inference_precedence() -> None:
    assert (
        _infer_channel(
            explicit=OpenAgentUpdateChannel.DEV, metadata=None, current_version="0.1.6rc4"
        )
        is OpenAgentUpdateChannel.DEV
    )
    meta = InstallMetadata(channel=OpenAgentUpdateChannel.STABLE)
    assert _infer_channel(explicit=None, metadata=meta, current_version="0.1.6rc4") is (
        OpenAgentUpdateChannel.STABLE
    )
    assert _infer_channel(explicit=None, metadata=None, current_version="0.1.6rc4") is (
        OpenAgentUpdateChannel.CANDIDATE  # prerelease → candidate
    )
    assert _infer_channel(explicit=None, metadata=None, current_version="0.2.0") is (
        OpenAgentUpdateChannel.STABLE  # final → stable
    )
    assert _infer_channel(explicit=None, metadata=None, current_version="garbage") is (
        OpenAgentUpdateChannel.CANDIDATE  # unparseable fails closed to candidate
    )


# ----------------------------------------------------------------- target resolution (spec §11)


def test_resolve_candidate_target_from_ls_remote_and_contents_api() -> None:
    init_py = base64.b64encode(b'__version__ = "0.1.6rc5"\n').decode()

    def runner(argv, timeout, limit, env, cwd):
        assert "ls-remote" in argv and "--heads" in argv and CANDIDATE_BRANCH in argv
        assert env.get("GIT_TERMINAL_PROMPT") == "0"  # credential-free
        return _result(stdout=f"{C}\trefs/heads/{CANDIDATE_BRANCH}\n")

    def fetcher(url, timeout, max_bytes):
        assert C in url and "contents/src/openagent/__init__.py" in url
        return {"content": init_py, "encoding": "base64"}

    target = resolve_vcs_target(OpenAgentUpdateChannel.CANDIDATE, runner=runner, fetcher=fetcher)
    assert target is not None
    assert target.commit_sha == C
    assert target.version == "0.1.6rc5"
    assert target.package_url == f"git+{OFFICIAL_HTTPS_REMOTE}@{C}"
    assert target.ref == CANDIDATE_BRANCH


def test_resolve_target_returns_none_when_branch_missing() -> None:
    target = resolve_vcs_target(
        OpenAgentUpdateChannel.CANDIDATE,
        runner=lambda *a: _result(returncode=0, stdout=""),
        fetcher=lambda *a: {},
    )
    assert target is None


def test_stable_ignores_prerelease_release() -> None:
    def fetcher(url, timeout, max_bytes):
        return {"tag_name": "v0.1.6rc4", "prerelease": True, "draft": False}

    assert (
        resolve_vcs_target(
            OpenAgentUpdateChannel.STABLE, runner=lambda *a: _result(), fetcher=fetcher
        )
        is None
    )


# ---------------------------------------------------------------------- channel plan logic (spec §12)


def _ahead(_base, _head):
    """Default ancestry oracle: the target is a descendant of what is installed.

    Injected explicitly so unit tests never reach the network for the compare API, and so a test
    that cares about ancestry has to say so.
    """

    return "ahead"


def _channel_check(active, *, current, installed_commit, target, **kw):
    return check_self_update(
        current_version=current,
        active_executable=str(active),
        direct_url=_vcs_direct_url(installed_commit),
        metadata=kw.pop("metadata", None),
        target_resolver=lambda ch: target,
        compare=kw.pop("compare", _ahead),
        **kw,
    )


def test_same_version_new_commit_is_an_update(tmp_path, fake_uv) -> None:
    active = _active(tmp_path / "bin" / "openagent")
    plan = _channel_check(active, current="0.1.6rc4", installed_commit=A, target=_target(B))
    assert plan.update_available is True
    assert plan.reason == "newer_commit"
    assert plan.installed_commit == A
    assert plan.target_commit == B


def test_same_commit_is_up_to_date(tmp_path, fake_uv) -> None:
    active = _active(tmp_path / "bin" / "openagent")
    plan = _channel_check(active, current="0.1.6rc4", installed_commit=A, target=_target(A))
    assert plan.can_update is True
    assert plan.update_available is False
    assert plan.reason == "up_to_date"


def test_newer_version_is_an_update(tmp_path, fake_uv) -> None:
    active = _active(tmp_path / "bin" / "openagent")
    plan = _channel_check(
        active, current="0.1.6rc4", installed_commit=A, target=_target(B, version="0.1.6rc5")
    )
    assert plan.update_available is True
    assert plan.reason == "newer_version"


def test_downgrade_is_blocked_without_flag(tmp_path, fake_uv) -> None:
    active = _active(tmp_path / "bin" / "openagent")
    plan = _channel_check(
        active, current="0.1.6rc4", installed_commit=A, target=_target(B, version="0.1.5")
    )
    assert plan.can_update is False
    assert plan.is_downgrade is True
    assert plan.reason == "downgrade_blocked"
    assert "--allow-downgrade" in plan.detail


def test_downgrade_allowed_with_flag(tmp_path, fake_uv) -> None:
    active = _active(tmp_path / "bin" / "openagent")
    plan = _channel_check(
        active,
        current="0.1.6rc4",
        installed_commit=A,
        target=_target(B, version="0.1.5"),
        allow_downgrade=True,
    )
    assert plan.can_update is True
    assert plan.reason == "downgrade"


def test_downgrade_baseline_uses_last_accepted_version(tmp_path, fake_uv) -> None:
    """A rollback attack cannot walk the install back one accepted version at a time (spec §12)."""

    active = _active(tmp_path / "bin" / "openagent")
    meta = InstallMetadata(last_accepted_version="0.1.6rc4", last_accepted_commit=A)
    plan = _channel_check(
        active,
        current="0.1.6rc3",  # running an older build...
        installed_commit=A,
        target=_target(B, version="0.1.6rc3"),  # ...target equals it, but we already accepted rc4
        metadata=meta,
    )
    assert plan.is_downgrade is True
    assert plan.reason == "downgrade_blocked"


def test_uv_missing_blocks_channel_update(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("openagent.services.self_update.shutil.which", lambda *a, **k: None)
    active = _active(tmp_path / "bin" / "openagent")
    plan = _channel_check(active, current="0.1.6rc4", installed_commit=A, target=_target(B))
    assert plan.can_update is False
    assert plan.reason == "uv_missing"


def test_no_target_available_is_reported(tmp_path, fake_uv) -> None:
    active = _active(tmp_path / "bin" / "openagent")
    plan = _channel_check(active, current="0.1.6rc4", installed_commit=A, target=None)
    assert plan.can_update is False
    assert plan.reason == "no_target"


# ------------------------------------------------------------------- install.json store (spec §8)


def test_install_metadata_roundtrip_and_mode(tmp_path) -> None:
    path = tmp_path / "install.json"
    meta = InstallMetadata(
        installed_version="0.1.6rc4",
        installed_commit=A,
        last_accepted_version="0.1.6rc4",
        last_accepted_commit=A,
        python="3.12",
    )
    write_install_metadata(meta, path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    loaded = read_install_metadata(path)
    assert loaded is not None
    assert loaded.installed_commit == A
    assert loaded.channel is OpenAgentUpdateChannel.CANDIDATE
    assert loaded.updated_at is not None


def test_install_metadata_preserves_unknown_fields(tmp_path) -> None:
    path = tmp_path / "install.json"
    path.write_text(json.dumps({"schema_version": 1, "future_key": "keep-me"}), encoding="utf-8")
    write_install_metadata(InstallMetadata(installed_commit=B), path)
    on_disk = json.loads(path.read_text())
    assert on_disk["future_key"] == "keep-me"
    assert on_disk["installed_commit"] == B


def test_install_metadata_malformed_is_quarantined(tmp_path) -> None:
    path = tmp_path / "install.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_install_metadata(path) is None
    assert path.with_suffix(".json.corrupt").exists()


def test_install_metadata_repository_cannot_be_widened(tmp_path) -> None:
    path = tmp_path / "install.json"
    path.write_text(json.dumps({"repository": "attacker/evil"}), encoding="utf-8")
    write_install_metadata(InstallMetadata(installed_commit=A, repository="attacker/evil"), path)
    on_disk = json.loads(path.read_text())
    assert on_disk["repository"] == OFFICIAL_REPOSITORY


def test_install_metadata_path_honors_openagent_home(tmp_path) -> None:
    p = install_metadata_path({"OPENAGENT_HOME": str(tmp_path / "oa")})
    assert p == tmp_path / "oa" / "install.json"


# ------------------------------------------------------------------------- CLI contract (spec §19)


def _cli_channel_plan(active: Path):
    from openagent.services.self_update import SelfUpdatePlan

    url = f"git+{OFFICIAL_HTTPS_REMOTE}@{B}"
    return SelfUpdatePlan(
        current_version="0.1.6rc4",
        latest_version="0.1.6rc4",
        source="official-github-vcs",
        active_executable=str(active),
        resolved_executable=str(active),
        check_method="official-github-vcs",
        update_available=True,
        can_update=True,
        channel=OpenAgentUpdateChannel.CANDIDATE,
        channel_ref=CANDIDATE_BRANCH,
        installed_commit=A,
        target_commit=B,
        package_url=url,
        commands=[
            [
                "/uv",
                "tool",
                "install",
                "--force",
                "--reinstall",
                "--refresh",
                "--python",
                "3.12",
                url,
            ]
        ],
        reason="newer_commit",
        detail="refresh",
    )


def test_cli_check_json_stable_schema(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from openagent.cli.app import app

    active = _active(tmp_path / "bin" / "openagent")
    monkeypatch.setattr(
        "openagent.services.self_update.check_self_update", lambda **_kw: _cli_channel_plan(active)
    )
    out = CliRunner().invoke(app, ["update", "--check", "--json"])
    assert out.exit_code == 0, out.output
    payload = json.loads(out.stdout)
    assert payload["channel"] == "candidate"
    assert payload["current"]["commit"] == A
    assert payload["target"]["commit"] == B
    assert payload["update_available"] is True
    assert payload["reason"] == "newer_commit"


def test_cli_channel_option_is_threaded(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from openagent.cli.app import app

    active = _active(tmp_path / "bin" / "openagent")
    seen = {}

    def fake_check(**kw):
        seen.update(kw)
        return _cli_channel_plan(active)

    monkeypatch.setattr("openagent.services.self_update.check_self_update", fake_check)
    out = CliRunner().invoke(app, ["update", "--channel", "dev", "--check"])
    assert out.exit_code == 0, out.output
    assert seen["channel"] is OpenAgentUpdateChannel.DEV


def test_cli_rejects_unknown_channel(monkeypatch) -> None:
    from typer.testing import CliRunner

    from openagent.cli.app import app

    out = CliRunner().invoke(app, ["update", "--channel", "banana", "--check"])
    assert out.exit_code == 1
    assert "unknown channel" in out.output.lower()


def test_cli_dry_run_shows_channel_install_command(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from openagent.cli.app import app

    active = _active(tmp_path / "bin" / "openagent")
    monkeypatch.setattr(
        "openagent.services.self_update.check_self_update", lambda **_kw: _cli_channel_plan(active)
    )
    out = CliRunner().invoke(app, ["update", "--dry-run"])
    assert out.exit_code == 0, out.output
    # rich wraps the long install command across lines; compare with whitespace collapsed.
    collapsed = "".join(out.output.split())
    assert f"git+{OFFICIAL_HTTPS_REMOTE}@{B}" in collapsed
    assert "wouldrun" in collapsed
