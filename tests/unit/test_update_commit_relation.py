"""Commit ancestry for same-version updates (spec §3.2).

The hole this closes: version strings are not ordering. During a release candidate every commit
on the channel reports the same ``0.1.6rc5``, so "same version, different commit" was treated as
an unconditional *refresh* — which happily installs a commit that is **older** than the one you
are running, or one from an unrelated history. Ordering has to come from the commit graph.
"""

from __future__ import annotations

import pytest

from openagent.services.update_safety import (
    CommitRelation,
    classify_commit_relation,
    relation_permits_install,
)

A = "a" * 40  # installed
B = "b" * 40  # a descendant of A
C = "c" * 40  # unrelated history


def _oracle(mapping):
    """An ancestry oracle over ``(base, head) -> status`` pairs; unknown pairs raise."""

    def compare(base: str, head: str) -> str | None:
        return mapping.get((base, head))

    return compare


# ------------------------------------------------------------------ classification


def test_same_commit_is_same() -> None:
    assert classify_commit_relation(installed=A, target=A, compare=_oracle({})) is (
        CommitRelation.SAME
    )


def test_descendant_target_is_ahead() -> None:
    relation = classify_commit_relation(installed=A, target=B, compare=_oracle({(A, B): "ahead"}))
    assert relation is CommitRelation.AHEAD


def test_ancestor_target_is_behind() -> None:
    relation = classify_commit_relation(installed=B, target=A, compare=_oracle({(B, A): "behind"}))
    assert relation is CommitRelation.BEHIND


def test_unrelated_target_is_diverged() -> None:
    relation = classify_commit_relation(
        installed=A, target=C, compare=_oracle({(A, C): "diverged"})
    )
    assert relation is CommitRelation.DIVERGED


def test_missing_history_is_unknown() -> None:
    """No answer from the oracle is not permission to proceed."""

    relation = classify_commit_relation(installed=A, target=B, compare=_oracle({}))
    assert relation is CommitRelation.UNKNOWN


def test_oracle_failure_is_unknown() -> None:
    def boom(base, head):
        raise OSError("network down")

    assert classify_commit_relation(installed=A, target=B, compare=boom) is CommitRelation.UNKNOWN


def test_absent_baseline_is_unknown() -> None:
    """With no recorded commit there is nothing to order against — fail closed, not open."""

    relation = classify_commit_relation(installed=None, target=B, compare=_oracle({}))
    assert relation is CommitRelation.UNKNOWN


# ------------------------------------------------------------------ decision matrix


@pytest.mark.parametrize(
    ("relation", "allowed", "allowed_with_downgrade"),
    [
        (CommitRelation.SAME, True, True),
        (CommitRelation.AHEAD, True, True),
        (CommitRelation.BEHIND, False, True),
        (CommitRelation.DIVERGED, False, True),
        (CommitRelation.UNKNOWN, False, False),
    ],
)
def test_relation_decision_matrix(relation, allowed, allowed_with_downgrade) -> None:
    assert relation_permits_install(relation, allow_downgrade=False) is allowed
    assert relation_permits_install(relation, allow_downgrade=True) is allowed_with_downgrade


def test_unknown_is_never_unlocked_by_allow_downgrade() -> None:
    """``--allow-downgrade`` says "go backwards deliberately", not "install anything"."""

    assert relation_permits_install(CommitRelation.UNKNOWN, allow_downgrade=True) is False


# ------------------------------------------------------------------ end-to-end through the plan
#
# The scenarios the spec calls out by name (§3.2). The same-version pair is the one that mattered:
# during a release candidate every commit reports the same version, so version comparison alone
# cannot tell "the next rc build" from "the rc build you already replaced".


@pytest.fixture
def fake_uv(monkeypatch, tmp_path):
    """A resolvable ``uv`` on PATH, so plans reach the ancestry decision instead of ``uv_missing``."""

    from tests.unit.test_self_update_channels import _active

    uv = _active(tmp_path / "uvbin" / "uv")
    monkeypatch.setattr(
        "openagent.services.self_update.shutil.which",
        lambda name, path=None: str(uv) if name == "uv" else None,
    )
    return str(uv)


def _plan_for(tmp_path, fake_uv, *, installed, target_commit, version, **kw):
    from openagent.services.self_update import check_self_update
    from tests.unit.test_self_update_channels import _active, _target, _vcs_direct_url

    active = _active(tmp_path / "bin" / "openagent")
    return check_self_update(
        current_version=kw.pop("current", "0.1.6rc5"),
        active_executable=str(active),
        direct_url=_vcs_direct_url(installed),
        metadata=kw.pop("metadata", None),
        target_resolver=lambda _ch: _target(target_commit, version=version),
        **kw,
    )


def test_plan_same_version_forward_commit_updates(tmp_path, fake_uv) -> None:
    plan = _plan_for(
        tmp_path,
        fake_uv,
        installed=A,
        target_commit=B,
        version="0.1.6rc5",
        compare=lambda base, head: "ahead",
    )
    assert plan.can_update is True
    assert plan.update_available is True
    assert plan.reason == "newer_commit"
    assert plan.commit_relation is CommitRelation.AHEAD


def test_plan_same_version_backward_commit_is_blocked(tmp_path, fake_uv) -> None:
    """The regression: identical version, older commit. Previously installed as a "refresh"."""

    plan = _plan_for(
        tmp_path,
        fake_uv,
        installed=B,
        target_commit=A,
        version="0.1.6rc5",
        compare=lambda base, head: "behind",
    )
    assert plan.can_update is False
    assert plan.commit_relation is CommitRelation.BEHIND
    assert plan.reason == "commit_behind_blocked"
    assert "--allow-downgrade" in plan.detail


def test_plan_unrelated_commit_is_blocked(tmp_path, fake_uv) -> None:
    plan = _plan_for(
        tmp_path,
        fake_uv,
        installed=A,
        target_commit=C,
        version="0.1.6rc5",
        compare=lambda base, head: "diverged",
    )
    assert plan.can_update is False
    assert plan.commit_relation is CommitRelation.DIVERGED
    assert plan.reason == "commit_diverged_blocked"


def test_plan_missing_history_is_blocked(tmp_path, fake_uv) -> None:
    plan = _plan_for(
        tmp_path,
        fake_uv,
        installed=A,
        target_commit=B,
        version="0.1.6rc5",
        compare=lambda base, head: None,
    )
    assert plan.can_update is False
    assert plan.commit_relation is CommitRelation.UNKNOWN
    assert plan.reason == "commit_unknown"
    assert "--allow-downgrade" in plan.detail  # stated as *not* an escape hatch


def test_plan_same_commit_is_up_to_date(tmp_path, fake_uv) -> None:
    plan = _plan_for(
        tmp_path,
        fake_uv,
        installed=A,
        target_commit=A,
        version="0.1.6rc5",
        compare=lambda base, head: "identical",
    )
    assert plan.update_available is False
    assert plan.reason == "up_to_date"


def test_plan_backward_commit_allowed_with_explicit_downgrade(tmp_path, fake_uv) -> None:
    plan = _plan_for(
        tmp_path,
        fake_uv,
        installed=B,
        target_commit=A,
        version="0.1.6rc5",
        compare=lambda base, head: "behind",
        allow_downgrade=True,
    )
    assert plan.can_update is True
    assert plan.is_downgrade is True
    assert plan.commit_relation is CommitRelation.BEHIND


def test_plan_baseline_is_last_accepted_commit_not_running_commit(tmp_path, fake_uv) -> None:
    """Ancestry is measured from the newest commit we ever accepted, so a rollback cannot creep.

    Running an older binary does not lower the bar: the comparison base stays the accepted commit.
    """

    from openagent.services.self_update import InstallMetadata, OpenAgentUpdateChannel

    seen: list[tuple[str, str]] = []

    def compare(base, head):
        seen.append((base, head))
        return "behind"

    meta = InstallMetadata(
        manager="uv-tool",
        source="official-github-vcs",
        repository="yasirkaramandev/openagent",
        channel=OpenAgentUpdateChannel.CANDIDATE,
        installed_version="0.1.6rc5",
        installed_commit=A,
        last_accepted_version="0.1.6rc5",
        last_accepted_commit=B,  # we have accepted B, even though A is unpacked right now
    )
    plan = _plan_for(
        tmp_path,
        fake_uv,
        installed=A,
        target_commit=C,
        version="0.1.6rc5",
        metadata=meta,
        compare=compare,
    )
    assert seen == [(B, C)]  # compared against the accepted commit, not the running one
    assert plan.can_update is False
