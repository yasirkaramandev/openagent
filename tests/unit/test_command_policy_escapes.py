"""safe-edit must not auto-run escape hatches (spec §2).

``safe-edit`` is a **policy** boundary, not an OS sandbox (see SECURITY.md). That makes the policy the
*only* thing standing between an agent and the host, so it must not hand out general-purpose code
execution for free. Before v0.1.3 it did: the executable allowlist contained `python`, `node`, `ruby`,
`git`, `cat`, `find`, `cp`, `sed`, `tee`… so ``cat /etc/passwd`` and
``python -c "print(open('/etc/passwd').read())"`` were auto-approved (Decision.ALLOW) and ran with no
human in the loop.

Worse, the few escapes that *did* stop were stopped by accident: ``python -c "import os; ..."`` hit
APPROVAL only because the ``;`` inside the quoted Python source tripped a *shell*-metacharacter regex
— nothing to do with interpreters. Drop the semicolon and it ran.

These tests pin the real contract: under safe-edit, anything that can execute arbitrary code, touch
paths outside the workspace, or mutate global tool config requires an explicit approval.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openagent.core.permissions import DEVELOPMENT, SAFE_EDIT, get_profile
from openagent.security.command_policy import Decision, evaluate

#: Every one of these auto-ran (Decision.ALLOW) under safe-edit before v0.1.3.
INTERPRETER_ESCAPES = [
    # Reads a host file the workspace has no business seeing.
    "python -c \"print(open('/etc/passwd').read())\"",
    "python -c \"import pathlib; print(pathlib.Path('/etc/passwd').read_text())\"",
    'python3 -c "print(1)"',
    # Opens a socket: network egress through an allowlisted runtime.
    "python -c \"import socket; socket.create_connection(('example.com', 80))\"",
    "python3 -c \"import urllib.request; urllib.request.urlopen('http://example.com')\"",
    # Deletes host files through an allowlisted runtime.
    "node -e \"require('fs').rmSync('/tmp/example', {recursive:true, force:true})\"",
    "node --eval \"require('child_process').execSync('id')\"",
    "ruby -e \"puts File.read('/etc/passwd')\"",
    "php -r \"echo file_get_contents('/etc/passwd');\"",
    'perl -e "print 1"',
    # -m runs a module: still arbitrary code.
    "python -m http.server",
]

#: Mutating global tool configuration is a persistent, out-of-workspace side effect.
GIT_CONFIG_ESCAPES = [
    "git config --global user.name attacker",
    "git config --global core.pager id",
    "git config user.email attacker@example.com",
    # -c injects config for one command — e.g. a pager/hook that executes.
    "git -c core.pager=id log",
    "git -c include.path=/tmp/evil status",
]

#: Absolute paths outside the workspace, through otherwise-innocent tools.
ABSOLUTE_PATH_ESCAPES = [
    "cat /etc/passwd",
    "find /",
    "find /etc -name passwd",
    "cp /etc/passwd copied.txt",
    "mv /etc/hosts hosts.bak",
    "sed -i s/a/b/ /etc/hosts",
    "tee /etc/cron.d/evil",
    "head /etc/shadow",
    "tail /var/log/system.log",
    "ls /Users",
    "grep -r secret /home",
]

#: Package/build scripts are indirect arbitrary execution (package.json, Makefile).
INDIRECT_EXECUTION = [
    "npm run evil",
    "npm run-script evil",
    "yarn evil",
    "make install",
    "npx some-package",
]


def _decide(command: str, workspace: Path):
    return evaluate(
        command,
        network_allowed=False,
        workspace_root=workspace,
        profile=get_profile(SAFE_EDIT),
    )


def _decide_development(command: str, workspace: Path):
    """The same question under ``development``, the profile that still has an auto-allow tier."""

    return evaluate(
        command,
        network_allowed=False,
        workspace_root=workspace,
        profile=get_profile(DEVELOPMENT),
    )


@pytest.mark.parametrize("command", INTERPRETER_ESCAPES)
def test_interpreter_escapes_are_not_auto_approved(command: str, tmp_path: Path) -> None:
    decision = _decide(command, tmp_path).decision
    assert decision is not Decision.ALLOW, (
        f"safe-edit auto-ran a general-purpose interpreter: {command!r}"
    )


@pytest.mark.parametrize("command", GIT_CONFIG_ESCAPES)
def test_git_config_mutation_is_not_auto_approved(command: str, tmp_path: Path) -> None:
    decision = _decide(command, tmp_path).decision
    assert decision is not Decision.ALLOW, f"safe-edit auto-ran a git config mutation: {command!r}"


@pytest.mark.parametrize("command", ABSOLUTE_PATH_ESCAPES)
def test_absolute_paths_outside_the_workspace_are_rejected(command: str, tmp_path: Path) -> None:
    result = _decide(command, tmp_path)
    # §2.6: auto-reject, not merely "ask" — the path is provably outside the workspace.
    assert result.decision is Decision.DENY, (
        f"safe-edit did not reject an out-of-workspace absolute path: {command!r} "
        f"(got {result.decision.value})"
    )


@pytest.mark.parametrize("command", INDIRECT_EXECUTION)
def test_package_and_build_scripts_are_not_auto_approved(command: str, tmp_path: Path) -> None:
    decision = _decide(command, tmp_path).decision
    assert decision is not Decision.ALLOW, (
        f"safe-edit auto-ran an indirect script runner: {command!r}"
    )


def test_the_semicolon_was_never_the_protection(tmp_path: Path) -> None:
    """The same interpreter escape must be blocked with *and* without a shell metacharacter.

    Pre-v0.1.3, `python -c "import os; ..."` hit APPROVAL only because the `;` matched a shell-meta
    regex. The identical attack without a semicolon auto-ran. Both must now be gated on the fact that
    it is an interpreter, not on incidental punctuation.
    """

    with_semicolon = "python -c \"import os; os.system('id')\""
    without_semicolon = "python -c \"print(open('/etc/passwd').read())\""
    assert _decide(with_semicolon, tmp_path).decision is not Decision.ALLOW
    assert _decide(without_semicolon, tmp_path).decision is not Decision.ALLOW


# --------------------------------------------------------------------------- what must still work


def test_read_only_inspection_inside_the_workspace_still_runs(tmp_path: Path) -> None:
    """The policy must stay usable: workspace-relative inspection needs no approval.

    Checked under ``development``, because since v0.1.4 ``safe-edit`` auto-allows no generic command
    at all (spec §4.2) — its read-only work goes through the dedicated tools instead. ``development``
    is now the profile where "unattended inspection still works" is an observable property.
    """

    for command in ("git status", "git diff", "ls -la", "pwd", "cat README.md", "grep -r TODO src"):
        result = _decide_development(command, tmp_path)
        assert result.decision is Decision.ALLOW, f"{command!r} should not need approval"


def test_workspace_relative_paths_are_not_confused_with_escapes(tmp_path: Path) -> None:
    """A relative path inside the workspace must not trip the out-of-workspace clamp.

    The property under test is that the *clamp* stays quiet, so it is asserted directly — a
    ``DENY`` is the clamp firing. Auto-allow is a separate decision, checked under ``development``
    where it exists; using ALLOW alone as the signal would silently stop testing the clamp the
    moment a profile's auto-allow changed.
    """

    for command in ("cat src/main.py", "find . -name '*.py'", "grep -rn TODO ./src"):
        assert _decide(command, tmp_path).decision is not Decision.DENY, command
        assert _decide_development(command, tmp_path).decision is Decision.ALLOW, command


def test_mutating_file_commands_need_approval_even_inside_the_workspace(tmp_path: Path) -> None:
    """safe-edit's unattended surface is read-only *inspection*; edits go through the edit tools.

    ``cp``/``mv``/``sed -i`` inside the workspace are not catastrophic, but they are writes, and the
    agent already has ``write_file``/``apply_patch`` for that. Keeping the unattended set read-only
    means an escape has to get past an approval prompt, not just past a path check.
    """

    for command in ("cp a.txt b.txt", "mv a.txt b.txt", "sed -i s/a/b/ a.txt", "tee out.txt"):
        assert _decide(command, tmp_path).decision is Decision.APPROVAL, command


# --------------------------------------------------------------------------- Windows / traversal

#: Windows-absolute forms. The clamp recognises these on *every* platform, so a policy decision made
#: on a POSIX CI runner still refuses a Windows escape (and vice versa) — the check must not depend
#: on which OS happens to be evaluating it.
WINDOWS_ABSOLUTE_ESCAPES = [
    r"cat C:\Windows\System32\drivers\etc\hosts",
    r"cat C:/Windows/System32/config/SAM",
    r"cp C:\Users\victim\.ssh\known_hosts out.txt",
    r"find \\server\share",
    r"cat \\?\C:\Windows\win.ini",
    r"type D:\secrets.txt",
]

TRAVERSAL_ESCAPES = [
    "cat ../../../etc/passwd",
    "cat ../outside.txt",
    "find ../..",
    "cp ../../secret.txt here.txt",
]


@pytest.mark.parametrize("command", WINDOWS_ABSOLUTE_ESCAPES)
def test_windows_absolute_paths_are_rejected_on_every_platform(
    command: str, tmp_path: Path
) -> None:
    assert _decide(command, tmp_path).decision is Decision.DENY, (
        f"a Windows-absolute path escaped the workspace clamp: {command!r}"
    )


@pytest.mark.parametrize("command", TRAVERSAL_ESCAPES)
def test_relative_traversal_out_of_the_workspace_is_rejected(command: str, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert _decide(command, workspace).decision is Decision.DENY, (
        f"`..` traversal escaped the workspace clamp: {command!r}"
    )


def test_home_directory_shorthand_is_rejected(tmp_path: Path) -> None:
    assert _decide("cat ~/.aws/config", tmp_path).decision is Decision.DENY
    assert _decide("ls ~", tmp_path).decision is Decision.DENY


def test_symlinked_workspace_root_is_not_a_false_escape(tmp_path: Path) -> None:
    """A workspace reached through a symlink must not make every relative path look like an escape.

    macOS `/tmp` is itself a symlink to `/private/tmp`, so comparing an unresolved root against a
    resolved argument would reject ordinary in-workspace paths. Both sides are resolved.
    """

    real = tmp_path / "real_ws"
    real.mkdir()
    (real / "a.txt").write_text("x")
    link = tmp_path / "link_ws"
    link.symlink_to(real, target_is_directory=True)
    # "Not a false escape" means the clamp did not fire — assert that, not the auto-allow tier.
    assert _decide("cat a.txt", link).decision is not Decision.DENY
    assert _decide("cat ./a.txt", link).decision is not Decision.DENY
    assert _decide_development("cat a.txt", link).decision is Decision.ALLOW
    assert _decide_development("cat ./a.txt", link).decision is Decision.ALLOW


def test_full_access_still_permits_absolute_paths(tmp_path: Path) -> None:
    """full-access is explicitly unrestricted — the workspace clamp is for the guarded profiles."""

    from openagent.core.permissions import FULL_ACCESS

    result = evaluate(
        "cat /etc/passwd",
        network_allowed=True,
        workspace_root=tmp_path,
        profile=get_profile(FULL_ACCESS),
    )
    assert result.decision is not Decision.DENY
