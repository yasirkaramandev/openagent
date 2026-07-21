"""The single version authority (spec §4, §12).

Every version decision in OpenAgent routes through ``openagent.core.versioning``. These tests pin
the two properties that hand-rolled parsers kept getting wrong: PEP 440 prerelease ordering (a
release candidate is *older* than its release, not equal to it), and fail-closed handling of
anything unparseable (never silently "equal", "newer", or "at least").
"""

from __future__ import annotations

import pytest

from openagent.core.versioning import (
    canonical_version,
    compare_versions,
    is_newer,
    parse_version,
    version_at_least,
)

pytestmark = pytest.mark.unit


def test_prerelease_orders_below_its_release() -> None:
    assert parse_version("1.2.0rc1") < parse_version("1.2.0")
    assert parse_version("1.2.0rc1") != parse_version("1.2.0")


def test_full_prerelease_chain_orders_correctly() -> None:
    chain = ["1.2.0.dev1", "1.2.0a1", "1.2.0b1", "1.2.0rc1", "1.2.0"]
    parsed = [parse_version(v) for v in chain]
    assert parsed == sorted(parsed), "dev < a < b < rc < final must hold"
    # And each adjacent pair strictly increases.
    for older, newer in zip(chain, chain[1:], strict=False):
        assert is_newer(newer, older) is True
        assert is_newer(older, newer) is False


def test_post_release_is_newer_than_release() -> None:
    assert is_newer("1.2.0.post1", "1.2.0") is True


def test_v_prefix_and_semver_prerelease_normalise() -> None:
    assert canonical_version("v1.2.3") == canonical_version("1.2.3")
    assert parse_version("v1.2.3") == parse_version("1.2.3")
    # SemVer's dashed prerelease is mechanically translated, and still orders below the release.
    assert is_newer("0.5.0", "0.5.0-rc.2") is True


def test_noisy_version_line_is_extracted() -> None:
    assert canonical_version("openagent 0.1.6rc1") == "0.1.6rc1"
    assert canonical_version("codex-cli 0.142.5") == "0.142.5"
    assert canonical_version("claude 1.2.3 (Claude Code)") == "1.2.3"


def test_prerelease_is_not_normalised_away() -> None:
    """The exact regression: a prerelease must not collapse onto its release string."""

    assert canonical_version("0.1.6rc1") == "0.1.6rc1"
    assert canonical_version("0.1.6rc1") != canonical_version("0.1.6")


@pytest.mark.parametrize(
    ("candidate", "installed", "expected"),
    [
        ("1.2.0", "1.2.0rc1", True),
        ("1.2.0rc1", "1.2.0", False),
        ("1.2.1", "1.2.0", True),
        ("1.2.0", "1.2.0", False),
        ("not-a-version", "1.2.0", None),
        (None, "1.2.0", None),
        ("1.2.0", None, None),
    ],
)
def test_is_newer(candidate: str | None, installed: str | None, expected: bool | None) -> None:
    assert is_newer(candidate, installed) is expected


def test_compare_versions_three_valued() -> None:
    assert compare_versions("1.0.0", "2.0.0") == -1
    assert compare_versions("2.0.0", "2.0.0") == 0
    assert compare_versions("2.0.1", "2.0.0") == 1
    assert compare_versions("garbage", "2.0.0") is None
    assert compare_versions("2.0.0", "also-garbage") is None


def test_unparseable_is_never_equal_or_at_least() -> None:
    assert parse_version("not-a-version") is None
    assert canonical_version("not-a-version") is None
    # The safety property §12 rests on: an unparseable version does not silently satisfy a policy.
    assert version_at_least("not-a-version", "1.0.0") is None
    assert version_at_least("1.0.0", "not-a-version") is None


def test_version_at_least_boundaries() -> None:
    assert version_at_least("1.2.0", "1.2.0") is True
    assert version_at_least("1.2.1", "1.2.0") is True
    assert version_at_least("1.1.9", "1.2.0") is False
    # A prerelease does not meet a minimum set at the release.
    assert version_at_least("1.2.0rc1", "1.2.0") is False
