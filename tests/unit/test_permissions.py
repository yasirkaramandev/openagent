import pytest

from openagent.core.permissions import (
    DEVELOPMENT,
    FULL_ACCESS,
    READ_ONLY,
    SAFE_EDIT,
    get_profile,
    profile_names,
)


def test_all_profiles_present():
    assert set(profile_names()) == {READ_ONLY, SAFE_EDIT, DEVELOPMENT, FULL_ACCESS}


def test_read_only_cannot_edit():
    p = get_profile(READ_ONLY)
    assert p.can_edit_files is False
    assert "apply_patch" not in p.allowed_tools
    assert "read_file" in p.allowed_tools
    assert p.codex_sandbox == "read-only"


def test_safe_edit_maps_to_workspace_write():
    p = get_profile(SAFE_EDIT)
    assert p.can_edit_files is True
    assert p.network_allowed is False
    assert p.codex_sandbox == "workspace-write"
    assert p.claude_permission_mode == "acceptEdits"
    assert "apply_patch" in p.allowed_tools


def test_full_access_maps_to_danger():
    assert get_profile(FULL_ACCESS).codex_sandbox == "danger-full-access"


def test_unknown_profile_raises():
    with pytest.raises(KeyError):
        get_profile("nope")
