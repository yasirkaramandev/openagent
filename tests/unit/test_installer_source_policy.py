"""Which commits an installer may treat as a production install source (spec §3.5).

The hole: "is this commit in the official repository?" was tested by fetching the SHA directly.
GitHub serves every commit reachable from *any* advertised ref, so that test says yes to every
feature branch — an unreviewed WIP commit could be installed and recorded as ``channel: stable``.
Membership in the repository is not membership in a channel.

These are contract tests over the three installer scripts. They assert the policy is expressed in
each one, so a platform cannot silently drift; the behaviour of the shell logic itself is covered
by the CI installer matrix, which runs the real scripts end to end.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

INSTALLERS = {
    "setup.sh": (ROOT / "setup.sh").read_text(encoding="utf-8"),
    "setup.ps1": (ROOT / "setup.ps1").read_text(encoding="utf-8"),
    "setup.bat": (ROOT / "setup.bat").read_text(encoding="utf-8"),
}


@pytest.mark.parametrize("name", sorted(INSTALLERS))
def test_installer_resolves_a_channel_source_ref(name) -> None:
    """Every installer picks an explicit per-channel ref rather than trusting a bare SHA."""

    text = INSTALLERS[name]
    assert "release-candidate" in text
    assert "refs/heads/main" in text, "the dev channel must name main explicitly"
    assert "refs/tags/" in text, "the stable channel must resolve a published tag"


@pytest.mark.parametrize("name", sorted(INSTALLERS))
def test_installer_requires_channel_membership_not_repository_membership(name) -> None:
    """The commit must be *on the channel*, proved by ancestry or by being the tag itself."""

    text = INSTALLERS[name]
    assert "merge-base --is-ancestor" in text, (
        "candidate/dev membership must be an ancestry test against the channel tip, not a "
        "bare `git fetch <sha>` (which succeeds for any branch)"
    )
    assert "ls-remote --tags" in text, "stable membership must match a published tag"


@pytest.mark.parametrize("name", sorted(INSTALLERS))
def test_installer_rejects_feature_branch_commits_with_a_usable_message(name) -> None:
    text = INSTALLERS[name]
    assert "not on the" in text and "channel" in text
    assert "OPENAGENT_SETUP_LOCAL=1" in text, "the message must name the supported alternative"


@pytest.mark.parametrize("name", sorted(INSTALLERS))
def test_installer_supports_an_explicit_channel_override(name) -> None:
    """`dev` is never inferred from a version string; it has to be asked for."""

    assert "OPENAGENT_SETUP_CHANNEL" in INSTALLERS[name]


@pytest.mark.parametrize("name", sorted(INSTALLERS))
def test_installer_rejects_a_prerelease_tag_as_stable(name) -> None:
    """A published `v1.2.3rc1` tag is a candidate artifact, not the stable channel."""

    text = INSTALLERS[name]
    assert "rc" in text and ("a[0-9]" in text or r"a\d" in text)


@pytest.mark.parametrize("name", sorted(INSTALLERS))
def test_installer_records_channel_provenance(name) -> None:
    """All three platforms write install.json, so `openagent update` is channel-aware day one."""

    text = INSTALLERS[name]
    assert "install.json" in text
    assert "last_accepted_commit" in text
    assert "official-github-vcs" in text


def test_unix_installer_is_syntactically_valid() -> None:
    subprocess.run(["sh", "-n", str(ROOT / "setup.sh")], check=True)


def test_local_development_install_is_still_available_on_every_platform() -> None:
    """The escape hatch has to exist, or contributors cannot install their own work."""

    for name, text in INSTALLERS.items():
        assert "dev-local" in text, f"{name} lost its local-development install path"


# ------------------------------------------------------------------ CI coverage (spec §3.4)


def _ci_jobs():
    import yaml

    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]


def test_ci_tests_the_production_install_path_separately_from_the_developer_one() -> None:
    """Every installer job used to set OPENAGENT_SETUP_LOCAL=1 — only the dev path was covered."""

    jobs = _ci_jobs()
    assert {"installer-production", "installer-production-windows"} <= set(jobs)

    for name in ("installer-production", "installer-production-windows"):
        job = jobs[name]
        env = job.get("env") or {}
        assert "OPENAGENT_SETUP_LOCAL" not in env, (
            f"{name} must exercise the real production path, not the developer one"
        )


def test_production_install_job_asserts_vcs_provenance_and_channel_metadata() -> None:
    jobs = _ci_jobs()
    for name in ("installer-production", "installer-production-windows"):
        text = str(jobs[name])
        assert "direct_url.json" in text, f"{name} must prove PEP 610 VCS provenance"
        assert "install.json" in text, f"{name} must prove channel metadata was recorded"
        assert "CANDIDATE_SHA" in text, f"{name} must pin an exact commit"
        assert "last_accepted_commit" in text
        assert "doctor" in text


def test_production_install_job_proves_a_feature_branch_is_refused() -> None:
    for name in ("installer-production", "installer-production-windows"):
        text = str(_ci_jobs()[name])
        assert "refused as a production install source" in text


def test_production_install_job_covers_awkward_paths_and_custom_dirs() -> None:
    """Spaces, non-ASCII, custom UV_TOOL_DIR and OPENAGENT_HOME — where installers actually break."""

    unix = str(_ci_jobs()["installer-production"])
    assert "oa production" in unix, "a path containing a space"
    assert "kurulum-öçşğü" in unix, "a non-ASCII path"
    assert "UV_TOOL_DIR" in unix and "OPENAGENT_HOME" in unix

    windows = str(_ci_jobs()["installer-production-windows"])
    assert "oa production" in windows
    assert "UV_TOOL_DIR" in windows and "OPENAGENT_HOME" in windows


def test_production_install_job_checks_idempotency_and_an_untouched_checkout() -> None:
    unix = str(_ci_jobs()["installer-production"])
    assert "idempotent" in unix
    assert "status --porcelain" in unix, "a second install must not dirty the checkout"
