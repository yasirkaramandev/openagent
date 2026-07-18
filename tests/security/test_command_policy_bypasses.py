"""Wrapper, subcommand and option-value bypasses of the command policy (spec §4).

Each case here ran **unattended** under a guarded profile before v0.1.4. They share one root cause:
the policy screened ``argv[0]`` and then trusted the rest of the command line. A name tells you
nothing about behaviour — ``env`` runs whatever follows it, ``find -delete`` deletes, ``git branch``
mutates the moment ``-D`` appears, and a path written as ``--output=../../x`` never looked like a
path at all because it started with a dash.

The invariant these tests pin down is deliberately blunt: **no case below is ever ``ALLOW``**, under
any guarded profile. Whether the answer is ``DENY`` or ``APPROVAL`` is a judgement about how to
present the refusal; that it is not silent execution is the security property.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openagent.core.permissions import DEVELOPMENT, SAFE_EDIT, get_profile
from openagent.security.command_policy import Decision, evaluate

#: Every guarded profile. ``full-access`` is excluded on purpose: it is documented, explicit,
#: user-selected consent to run anything, and pretending otherwise would be theatre.
GUARDED = [SAFE_EDIT, DEVELOPMENT]

#: (command, what makes it dangerous)
BYPASSES: list[tuple[str, str]] = [
    # --- wrappers: the screened name is not the program that runs -------------------------------
    ('env python -c "print(1)"', "env runs the interpreter that follows it"),
    ('env FOO=bar node -e "1"', "env with an assignment still runs node"),
    ("xargs python", "xargs builds a command line and runs it"),
    ("nice python script.py", "nice runs another program"),
    ("timeout 5 python script.py", "timeout runs another program"),
    # --- find: a search tool with an exec and a delete ------------------------------------------
    ('find . -exec python -c "print(1)" {} +', "find -exec runs another program"),
    ("find . -execdir sh -c x {} +", "find -execdir runs another program"),
    ("find . -ok rm {} ;", "find -ok runs another program"),
    ("find . -okdir rm {} ;", "find -okdir runs another program"),
    ("find . -delete", "find -delete removes files"),
    ("find . -fprint ../../outside", "find -fprint writes to a file"),
    ("find . -fprintf ../../outside %p", "find -fprintf writes to a file"),
    ("find . -fls ../../outside", "find -fls writes to a file"),
    # --- git: read-only until an option makes it not ---------------------------------------------
    ("git branch -D somebranch", "git branch -D deletes a branch"),
    ("git tag -d sometag", "git tag -d deletes a tag"),
    ("git config --global user.name attacker", "git config mutates global state"),
    ("git -c core.pager=python log", "git -c injects a pager that executes"),
    ("git -C /etc log", "git -C escapes the workspace"),
    ("git update-index --refresh", "git update-index mutates the index"),
    ("git checkout main", "git checkout mutates the working tree"),
    ("git switch main", "git switch mutates the working tree"),
    ("git reset --hard", "git reset discards work"),
    ("git clean -fd", "git clean removes untracked files"),
    ("git apply patch.diff", "git apply mutates the working tree"),
    ("git commit -m x", "git commit mutates history"),
    ("git add .", "git add mutates the index"),
    # --- option values that hide a path -----------------------------------------------------------
    ("sort --output=../../outside input.txt", "--opt=value hid the path from the clamp"),
    ("sort --output ../../outside input.txt", "separate option value escaping the workspace"),
    ("sort -o/tmp/outside input.txt", "-oVALUE hid the path from the clamp"),
    ("sort -o /tmp/outside input.txt", "-o VALUE escaping the workspace"),
    ("git diff --output=../../outside", "git --output writes outside the workspace"),
    ("tree -o ../../outside", "tree -o writes to a file"),
    # --- in-place / program-spawning text tools ---------------------------------------------------
    ("sed -i s/a/b/ file.txt", "sed -i rewrites files in place"),
    ("tee ../../outside", "tee writes wherever it is pointed"),
    ("grep -f ../../outside pattern", "grep -f reads an arbitrary file"),
    # --- plain traversal, still refused ------------------------------------------------------------
    ("cat ../../outside", "path traversal"),
    ("cat /etc/passwd", "absolute path outside the workspace"),
    ("cat ~/.ssh/id_rsa", "home directory escape"),
]


@pytest.mark.parametrize("profile_name", GUARDED)
@pytest.mark.parametrize(("command", "why"), BYPASSES, ids=[c for c, _ in BYPASSES])
def test_bypass_never_runs_unattended(
    tmp_path: Path, profile_name: str, command: str, why: str
) -> None:
    result = evaluate(command, workspace_root=tmp_path, profile=get_profile(profile_name))
    assert result.decision is not Decision.ALLOW, (
        f"{command!r} was auto-allowed under {profile_name}: {why}"
    )


# --------------------------------------------------------------------------- Windows spellings


WINDOWS_BYPASSES = [
    r"cat C:\Windows\win.ini",
    r"cat \\server\share\secret",
    r"type C:\Windows\win.ini",
    r"cmd /c dir",
    r"powershell -Command Get-Content secret.txt",
    r"pwsh -Command Get-Content secret.txt",
    "where python",
    "start notepad.exe",
    r"sort --output=C:\Windows\Temp\x input.txt",
]


@pytest.mark.parametrize("profile_name", GUARDED)
@pytest.mark.parametrize("command", WINDOWS_BYPASSES)
def test_windows_spellings_reach_the_same_verdict(
    tmp_path: Path, profile_name: str, command: str
) -> None:
    """The security outcome must not depend on which platform's path syntax was used.

    These are checked on every platform on purpose: POSIX ``shlex`` eats backslashes, so a
    Windows-style path can arrive at the clamp already mangled into something that no longer looks
    absolute. Running the case everywhere is what catches that.
    """

    result = evaluate(command, workspace_root=tmp_path, profile=get_profile(profile_name))
    assert result.decision is not Decision.ALLOW


# --------------------------------------------------------------------------- the other direction


#: Ordinary read-only inspection that must keep working unattended under ``development``.
LEGITIMATE = [
    "ls",
    "ls -la",
    "pwd",
    "cat README.md",
    "head -n 20 file.txt",
    "head -20 file.txt",
    "tail -5 log.txt",
    "wc -l file.txt",
    "grep -rn TODO src",
    "rg -n TODO src",
    "find . -name *.py",
    "find . -type f -name *.md",
    "find src -maxdepth 2 -type d",
    "sort file.txt",
    "uniq -c file.txt",
    "cut -d: -f1 file.txt",
    "diff -u a.txt b.txt",
    "stat file.txt",
    "realpath .",
    "git status --short",
    "git diff --stat",
    "git log --oneline -n 5",
    "git show --name-only",
]


@pytest.mark.parametrize("command", LEGITIMATE)
def test_legitimate_inspection_still_runs_unattended(tmp_path: Path, command: str) -> None:
    """A boundary nobody can work behind gets switched off, so this half matters too."""

    result = evaluate(command, workspace_root=tmp_path, profile=get_profile(DEVELOPMENT))
    assert result.decision is Decision.ALLOW, f"{command!r} regressed to {result.reason}"


def test_safe_edit_auto_allows_no_generic_command(tmp_path: Path) -> None:
    """The preferred design (spec §4.2): under safe-edit there is no allowlist left to trick."""

    profile = get_profile(SAFE_EDIT)
    for command in LEGITIMATE:
        result = evaluate(command, workspace_root=tmp_path, profile=profile)
        assert result.decision is not Decision.ALLOW, (
            f"{command!r} ran unattended under safe-edit; read-only work belongs to the dedicated "
            "tools, which cannot be pointed at another program"
        )


def test_unknown_option_fails_closed(tmp_path: Path) -> None:
    """An option nobody thought about is not thereby safe."""

    result = evaluate(
        "ls --totally-new-option", workspace_root=tmp_path, profile=get_profile(DEVELOPMENT)
    )
    assert result.decision is Decision.APPROVAL
    assert "not an allowed option" in result.reason


def test_unknown_executable_fails_closed(tmp_path: Path) -> None:
    result = evaluate(
        "some-tool-nobody-listed --go", workspace_root=tmp_path, profile=get_profile(DEVELOPMENT)
    )
    assert result.decision is Decision.APPROVAL
