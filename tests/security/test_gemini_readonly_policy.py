"""Gemini CLI read-only is enforced by settings precedence, not by a guessed env var (spec §12).

The previous enforcement set ``GEMINI_EXCLUDE_TOOLS``. That variable is not in Gemini CLI's
documented contract, and an unrecognised environment variable is silently ignored — so "read-only"
was a label on a run that could still write files and spawn shells. A permission profile that the
CLI never receives is worse than no profile, because it is one the user was told they had.

What the CLI does document is a settings hierarchy in which *system* settings win over user and
project settings, and whose path can be redirected with ``GEMINI_CLI_SYSTEM_SETTINGS_PATH``. That
is the only layer a project's own ``.gemini/settings.json`` cannot widen, which is exactly the
property a security boundary needs: the workspace being operated on is attacker-influenced input.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from openagent.runtimes.cli.gemini import (
    MUTATING_TOOLS,
    READ_ONLY_TOOLS,
    SYSTEM_SETTINGS_ENV,
    permission_mapping,
    system_settings_document,
    system_settings_file,
)
from openagent.security.private_files import verify_private_directory, verify_private_file

# --------------------------------------------------------------------------- the document


def test_gemini_read_only_blocks_write_file() -> None:
    document = system_settings_document(permission_mapping("read-only"))
    assert "write_file" in document["tools"]["exclude"]
    assert "write_file" not in document["tools"]["core"]


def test_gemini_read_only_blocks_shell() -> None:
    document = system_settings_document(permission_mapping("read-only"))
    assert "run_shell_command" in document["tools"]["exclude"]
    assert "run_shell_command" not in document["tools"]["core"]


def test_read_only_is_an_allowlist_not_only_a_denylist() -> None:
    """A denylist alone lets a tool added in a future CLI release through by default."""

    document = system_settings_document(permission_mapping("read-only"))
    assert set(document["tools"]["core"]) == set(READ_ONLY_TOOLS)
    assert set(MUTATING_TOOLS) <= set(document["tools"]["exclude"])


def test_every_mutating_tool_is_excluded_for_read_only() -> None:
    document = system_settings_document(permission_mapping("read-only"))
    excluded = set(document["tools"]["exclude"])
    for tool in ("write_file", "replace", "run_shell_command"):
        assert tool in excluded


def test_telemetry_and_prompt_logging_are_off() -> None:
    """The prompt carries the user's code. It does not go to a vendor endpoint by default."""

    document = system_settings_document(permission_mapping("read-only"))
    assert document["telemetry"]["enabled"] is False
    assert document["telemetry"]["logPrompts"] is False


def test_a_writing_profile_does_not_get_a_read_only_allowlist() -> None:
    """The override must not silently break the profiles that are supposed to edit."""

    document = system_settings_document(permission_mapping("safe-edit"))
    assert "write_file" not in document.get("tools", {}).get("exclude", [])


# --------------------------------------------------------------------------- the file


def test_the_settings_file_is_private_and_removed_afterwards() -> None:
    """Privacy is asserted through the platform's own model, not through POSIX mode bits.

    The mode-bit form of this test passed on Linux and macOS and failed on Windows with
    ``0666`` — not because the file was less private there, but because ``stat.S_IMODE`` does not
    describe Windows security at all. Relaxing the assertion to accept ``0666`` would have turned
    a real check into a decorative one, so it asks
    :mod:`openagent.security.private_files` instead, which answers per platform. The
    platform-specific mechanics are covered in ``test_private_files.py``.
    """

    with system_settings_file(permission_mapping("read-only")) as path:
        assert path is not None
        verification = verify_private_file(path)
        assert verification.ok, f"settings file is not private: {verification.findings}"
        directory = verify_private_directory(path.parent)
        assert directory.ok, f"settings dir is not private: {directory.findings}"
        assert json.loads(path.read_text())["tools"]["exclude"]
        remembered = path
    assert not remembered.exists(), "the policy file outlived the run it applied to"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_settings_file_carries_posix_0600() -> None:
    """On POSIX the contract is still exactly 0600/0700, and stays asserted as such."""

    with system_settings_file(permission_mapping("read-only")) as path:
        assert path is not None
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL")
def test_the_settings_file_carries_a_private_windows_dacl() -> None:
    """On Windows the contract is a protected DACL naming nobody but this user and SYSTEM."""

    with system_settings_file(permission_mapping("read-only")) as path:
        assert path is not None
        verification = verify_private_file(path)
        assert verification.ok, verification.findings
        assert verification.inheritance_disabled is True
        assert verification.trustees


def test_the_environment_points_at_the_override() -> None:
    from openagent.runtimes.cli.gemini import apply_system_settings

    with system_settings_file(permission_mapping("read-only")) as path:
        env = apply_system_settings({}, path)
        assert env[SYSTEM_SETTINGS_ENV] == str(path)


def test_the_run_environment_carries_the_override_and_not_the_invented_var() -> None:
    """The enforcement the child process actually receives.

    Asserted on the environment rather than on the source text, so the check survives a comment
    that explains why the old variable is gone.
    """

    from openagent.runtimes.cli.gemini import apply_system_settings

    with system_settings_file(permission_mapping("read-only")) as path:
        env = apply_system_settings({}, path)

    assert "GEMINI_EXCLUDE_TOOLS" not in env
    assert SYSTEM_SETTINGS_ENV in env


def test_a_profile_that_constrains_nothing_sets_no_override() -> None:
    """Pointing the CLI at an empty system-settings file would override the user's real one."""

    from openagent.runtimes.cli.gemini import apply_system_settings

    with system_settings_file(permission_mapping("full")) as path:
        assert path is None
        assert apply_system_settings({}, path) == {}


# --------------------------------------------------------------------------- precedence


def test_gemini_project_settings_cannot_override_openagent_policy() -> None:
    """A workspace is attacker-influenced input; its settings must not re-enable a withheld tool.

    Asserted structurally, since the CLI is not installed here: OpenAgent's policy is written to the
    *system* layer, which the documented hierarchy places above project settings. Writing it to the
    user or workspace layer would be overridable by the repository being operated on.
    """

    with system_settings_file(permission_mapping("read-only")) as path:
        # Not inside the workspace, so a run cannot rewrite its own policy through a relative path.
        assert not str(path).startswith(os.getcwd())
        document = json.loads(path.read_text())
        assert "run_shell_command" in document["tools"]["exclude"]
