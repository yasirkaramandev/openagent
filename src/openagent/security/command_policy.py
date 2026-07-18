"""Command policy (spec §29, §2).

Before any shell command runs it is screened here. **This is a policy boundary, not an OS sandbox**
(SECURITY.md says so plainly): nothing below is enforced by the kernel. Because the policy is the
only thing between an agent and the host, it must not hand out general-purpose code execution.

The pre-v0.1.3 design got this exactly backwards. It called a broad executable list the "primary
boundary" and auto-approved everything on it — including ``python``, ``node``, ``ruby``, ``git``,
``cat``, ``find``, ``cp``, ``sed`` and ``tee``. So all of these ran with **no** human in the loop::

    cat /etc/passwd
    python -c "print(open('/etc/passwd').read())"
    python -c "import socket; socket.create_connection(('example.com', 80))"
    node -e "require('fs').rmSync('/tmp/x', {recursive:true, force:true})"
    git config --global user.name attacker

An allowlist of *interpreters* is not a boundary at all — it is a shell by another name. Worse, the
handful of escapes that did stop, stopped by accident: ``python -c "import os; ..."`` matched a
*shell*-metacharacter regex because of the ``;`` inside the quoted Python source. Remove the
semicolon and it ran.

v0.1.3 narrowed that list but kept its shape, and the shape was the bug. Screening ``argv[0]`` says
nothing about what the process will do, so every one of these still ran unattended::

    env python -c "..."             # env is "read-only"; python is what runs
    find . -exec python -c "..." +  # find is "read-only"; -exec runs anything
    find . -delete                  # a search tool that deletes
    git branch -D release           # "branch" is read-only until -D
    sort --output=../../outside f   # the clamp skipped tokens starting with "-"

The model now:

* **Under a guarded profile nothing runs unattended through a generic command string.** ``safe-edit``
  auto-allows *no* command; the read-only work an agent needs is served by dedicated tools
  (``read_file``, ``search_text``, ``git_status``, ``git_diff``) whose arguments are structured and
  cannot name a second program. With nothing auto-allowed there is no allowlist left to trick.
* **Where auto-allow does exist** (``development``), it is granted by an explicit per-executable
  validator that must account for every option it accepts — see ``command_validators``. An unknown
  option fails closed, and mutating or program-spawning options are named and refused.
* **Anything that executes code** — interpreters, wrappers, package/build scripts — requires an
  explicit approval. ``run_tests`` is separately gated by ``security.project_code``, because a
  validated ``pytest -q`` argv still executes whatever test files the agent just wrote.
* **Arguments that resolve outside the workspace are rejected outright** (spec §2.6), including
  paths hidden inside an option value (``--output=/etc/x``, ``-o/etc/x``).
* **Shell metacharacters and shell interpreters** still require approval — but they are now
  defense-in-depth, not the load-bearing check they accidentally were.

Outcomes:

* ``DENY`` — categorically forbidden (push, publish, sudo, credential reads, out-of-workspace paths).
* ``APPROVAL`` — plausible but privileged; a human must say yes first.
* ``ALLOW`` — narrow, verified, read-only-in-workspace (or the profile is explicitly unrestricted).
"""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..core.permissions import (
    AUTO_ALLOW_ALL,
    AUTO_ALLOW_BUILD,
    AUTO_ALLOW_INSPECT,
    AUTO_ALLOW_NONE,
    PermissionProfile,
)
from .command_validators import (
    WRAPPER_EXECUTABLES,
    get_validator,
    option_value_candidates,
    validate_argv,
)


class Decision(str, Enum):
    ALLOW = "allow"
    APPROVAL = "approval"
    DENY = "deny"


class Purpose(str, Enum):
    """Why the command is being run — different capabilities, different auto-allow sets."""

    #: A generic ``run_command`` call. Auto-allow is read-only inspection only.
    COMMAND = "command"
    #: A ``run_tests`` call. Auto-allow additionally covers the structured test/build runners.
    TEST = "test"


@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    reason: str = ""
    #: Structured argv for ``shell=False`` execution (empty when the command needs a shell).
    argv: tuple[str, ...] = field(default_factory=tuple)
    #: True when the command uses shell operators / an interpreter and can only run via a shell.
    needs_shell: bool = False

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


# --------------------------------------------------------------------------- narrow auto-allow sets

#: Read-only inspection is no longer a name list. Which executables may run unattended, and with
#: which options, is declared per executable in ``command_validators.VALIDATORS`` — a name on its
#: own says nothing about whether ``find -delete`` or ``sort --output=/etc/x`` is about to happen.

#: Structured test/build runners, reachable only through ``run_tests`` (spec §2.3). These DO execute
#: the project's own code — that is the point of the capability — so they are a named, explicit
#: surface rather than something a generic ``run_command`` can reach unattended.
_TEST_RUNNERS: dict[str, frozenset[str] | None] = {
    "pytest": None,
    "tox": None,
    "nox": None,
    "ruff": None,
    "mypy": None,
    "black": None,
    "isort": None,
    "flake8": None,
    "pylint": None,
    "eslint": None,
    "prettier": None,
    "tsc": None,
    "jest": None,
    "mocha": None,
    "vitest": None,
    "go": frozenset({"test", "vet", "build"}),
    "cargo": frozenset({"test", "check", "clippy", "build", "fmt"}),
    "dotnet": frozenset({"test", "build"}),
    "gradle": frozenset({"test", "build", "check"}),
    "gradlew": frozenset({"test", "build", "check"}),
    "mvn": frozenset({"test", "verify", "compile"}),
    "rake": frozenset({"test", "spec"}),
    "make": frozenset({"test", "check", "lint", "build"}),
}

#: Package/build tooling that development (but not safe-edit) may run unattended. Network use is
#: gated separately by ``_NETWORK_PATTERNS``.
_BUILD_TOOLS: dict[str, frozenset[str] | None] = {
    "pip": None,
    "pip3": None,
    "pipx": None,
    "uv": None,
    "poetry": None,
    "npm": None,
    "pnpm": None,
    "yarn": None,
    "bun": None,
    "npx": None,
    "cmake": None,
    "ninja": None,
    "meson": None,
    "gem": None,
    "bundle": None,
    "composer": None,
    "mkdir": None,
    "touch": None,
    "cp": None,
    "mv": None,
    "tr": None,
}
# NOTE: ``sed``, ``awk``, ``tee``, ``env`` and ``xargs`` were removed from this table in v0.1.4.
# Each of them either runs another program or writes to an arbitrary path, so no permission tier
# auto-allows them any more — see ``WRAPPER_EXECUTABLES``.

#: General-purpose language runtimes. These are NOT "known-safe executables" (spec §2.9): each one is
#: a complete programming environment with unrestricted file and socket access. Naming them here is
#: only so the refusal can say *why*; they are never auto-allowed under a guarded profile.
_GENERAL_RUNTIMES = frozenset(
    {
        "python",
        "python3",
        "py",
        "node",
        "deno",
        "bun",
        "ruby",
        "perl",
        "php",
        "java",
        "scala",
        "Rscript",
        "lua",
        "osascript",
    }
)

#: Shell interpreters. Allowing them defeats ``shell=False`` argv screening entirely.
_SHELL_INTERPRETERS = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "pwsh", "powershell", "cmd"}
)

#: Characters/tokens that force a real shell (pipes, redirects, subshells, chaining).
_SHELL_META = re.compile(r"[|&;<>`]|\$\(|\$\{|>>|\|\||&&|\n")

#: ``git`` invocations that mutate configuration or inject it for one command. `-c core.pager=…` and
#: `-c include.path=…` are code-execution vectors, and `config --global` escapes the workspace.
_GIT_CONFIG = re.compile(r"^\s*git\s+(-c\b|--exec-path|config\b|-C\b)", re.IGNORECASE)


# --------------------------------------------------------------------------- denylist

_DENY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bgit\s+push\b"), "git push is not allowed"),
    (re.compile(r"\bnpm\s+publish\b"), "npm publish is not allowed"),
    (re.compile(r"\b(pip|twine)\s+upload\b"), "package upload is not allowed"),
    (re.compile(r"\bdocker\s+login\b"), "docker login is not allowed"),
    (re.compile(r"\b(aws|gcloud|az)\s+.*\blogin\b"), "cloud CLI login is not allowed"),
    (re.compile(r"\bsudo\b"), "sudo is not allowed"),
    (re.compile(r"\bsecurity\s+find-generic-password\b"), "keychain access is not allowed"),
    (
        re.compile(r"(?i)\b(cat|less|more|head|tail|bat)\b[^\n|;&]*\.env\b"),
        "reading .env is not allowed",
    ),
    (re.compile(r"(?i)id_rsa|id_ed25519|\.ssh/"), "SSH private key access is not allowed"),
    (
        re.compile(r"(?i)\b(cat|less|more)\b[^\n|;&]*(credentials|\.aws/)"),
        "reading credentials is not allowed",
    ),
]

_APPROVAL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+-[rfRF]"), "recursive/forced delete"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "hard reset discards changes"),
    (re.compile(r"\bgit\s+clean\b"), "git clean removes untracked files"),
    (re.compile(r"\b(mkfs|dd)\b"), "disk-level operation"),
    (re.compile(r"\bchmod\s+-R\b"), "recursive permission change"),
    (re.compile(r">\s*/dev/sd"), "raw device write"),
]

#: Network-oriented commands. Under a no-network profile these are gated behind *approval* — a policy
#: boundary, NOT a kernel one. Nothing here stops an approved process from opening a socket.
_NETWORK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(curl|wget|nc|ncat|telnet|ssh|scp|rsync)\b"),
    re.compile(r"\b(pip|pip3)\s+install\b"),
    re.compile(r"\b(npm|pnpm|yarn)\s+(install|add|ci)\b"),
    re.compile(r"\b(apt|apt-get|brew|dnf|yum)\s+install\b"),
    re.compile(r"\bgit\s+(clone|fetch|pull)\b"),
]

#: Windows absolute forms: ``C:\x``, ``C:/x``, ``\\server\share``, ``\\?\C:\x``.
_WIN_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def split_command(command: str) -> list[str]:
    """Best-effort tokenization for inspection/logging."""

    try:
        return shlex.split(command, posix=not sys.platform.startswith("win"))
    except ValueError:
        return command.split()


def _basename(executable: str) -> str:
    return executable.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def _looks_absolute(token: str) -> bool:
    return token.startswith(("/", "~")) or bool(_WIN_ABSOLUTE.match(token))


def escapes_workspace(token: str, workspace_root: Path) -> bool:
    """Whether a single argument resolves outside ``workspace_root`` (spec §2.6).

    Fails **closed**: anything that cannot be resolved is treated as an escape. Only arguments are
    checked, never ``argv[0]`` — that is the executable and is resolved via PATH, not the workspace.
    """

    if not token or token.startswith("-"):
        return False  # a flag, not a path
    if token.startswith("~"):
        return True  # the home directory is outside the workspace by definition
    has_traversal = ".." in re.split(r"[\\/]", token)
    if not _looks_absolute(token) and not has_traversal:
        return False  # an ordinary relative path stays inside by construction
    try:
        root = workspace_root.resolve()
        candidate = Path(token) if _looks_absolute(token) else (workspace_root / token)
        resolved = candidate.resolve()
    except (OSError, ValueError, RuntimeError):
        return True  # unresolvable → fail closed
    return resolved != root and root not in resolved.parents


def _path_candidates(command: str, argv: tuple[str, ...]) -> list[str]:
    """Argument tokens to run the workspace clamp over.

    Three things have to be caught here, and each one was a real escape:

    * Both the shlex-split argv **and** a raw whitespace split are checked, because POSIX shlex
      treats ``\\`` as an escape character: on Linux/macOS it silently rewrites
      ``C:\\Windows\\System32`` into ``C:WindowsSystem32``, which no longer looks absolute and would
      sail straight past the clamp. The raw split preserves the backslashes.
    * Values **embedded in an option token** (``--output=../../x``, ``-o/etc/passwd``) are extracted,
      because the clamp used to skip anything starting with ``-`` — so writing the path into the
      option itself walked straight through it.
    """

    tokens = list(argv[1:])
    for raw in command.split()[1:]:
        stripped = raw.strip("\"'")
        if stripped:
            tokens.append(stripped)
    # An option's value is still a path even when it is glued to the option name.
    for token in list(tokens):
        tokens.extend(option_value_candidates(token))
    return tokens


def _first_subcommand(argv: tuple[str, ...]) -> str | None:
    for token in argv[1:]:
        if not token.startswith("-"):
            return token
    return None


def _validate_inspection(
    exe: str, argv: tuple[str, ...], *, clamp: bool, workspace_root: Path | None
) -> PolicyResult | None:
    """Auto-allow ``exe`` only if its validator accepts every argument it was given.

    Returns ``None`` when there is no validator for ``exe`` (the caller keeps looking), an ``ALLOW``
    when the whole argv checks out, and an ``APPROVAL`` carrying the validator's reason otherwise.
    A validator that refuses never falls through to another auto-allow path.
    """

    spec = get_validator(exe)
    if spec is None:
        return None
    outcome = validate_argv(
        spec, argv, clamp=clamp, workspace_root=workspace_root, escapes=escapes_workspace
    )
    if outcome.ok:
        return PolicyResult(Decision.ALLOW, argv=argv)
    return PolicyResult(
        Decision.APPROVAL,
        f"{exe}: {outcome.reason}; running it requires approval",
        argv=argv,
    )


def _in_table(exe: str, argv: tuple[str, ...], table: dict[str, frozenset[str] | None]) -> bool:
    if exe not in table:
        return False
    allowed_subcommands = table[exe]
    if allowed_subcommands is None:
        return True
    sub = _first_subcommand(argv)
    return sub is not None and sub in allowed_subcommands


def evaluate(
    command: str,
    *,
    network_allowed: bool = False,
    workspace_root: Path | None = None,
    profile: PermissionProfile | None = None,
    purpose: Purpose = Purpose.COMMAND,
) -> PolicyResult:
    """Screen a raw shell command string and return a structured decision.

    ``profile`` decides how much runs unattended; when omitted the **most conservative** guarded tier
    is assumed. Defaulting to permissive is precisely how the pre-v0.1.3 hole existed, so the default
    here fails safe rather than convenient.
    """

    auto_allow = profile.command_auto_allow if profile else AUTO_ALLOW_INSPECT
    clamp = profile.restrict_to_workspace if profile else True

    normalized = command.strip()
    if not normalized:
        return PolicyResult(Decision.DENY, "empty command")

    # 1. Categorical denials always win, for every profile.
    for pattern, reason in _DENY_PATTERNS:
        if pattern.search(normalized):
            return PolicyResult(Decision.DENY, reason)

    argv = tuple(split_command(normalized))
    if not argv:
        return PolicyResult(Decision.DENY, "empty command")

    # 2. An explicitly unrestricted profile (full-access) stops here: the user has consented.
    if auto_allow == AUTO_ALLOW_ALL:
        if _SHELL_META.search(normalized):
            return PolicyResult(Decision.ALLOW, argv=argv, needs_shell=True)
        return PolicyResult(Decision.ALLOW, argv=argv)

    # 3. Out-of-workspace arguments are rejected outright — an allowlisted tool must not become a
    #    read/write primitive for the rest of the filesystem (spec §2.6). This runs *before* the
    #    per-profile gates so that an escaping path is refused rather than offered for approval:
    #    "would you like to let the agent read /etc/shadow?" is not a question worth asking.
    if clamp and workspace_root is not None:
        for token in _path_candidates(normalized, argv):
            if escapes_workspace(token, workspace_root):
                return PolicyResult(
                    Decision.DENY,
                    f"{token!r} resolves outside the workspace; this profile confines commands to "
                    f"{workspace_root}",
                    argv=argv,
                )

    # 4. No unattended command execution at all for this profile. ``safe-edit`` lives here: its
    #    read-only inspection is served by dedicated tools (read_file, search_text, git_status,
    #    git_diff) that take structured arguments, so a generic command string has nothing left to
    #    auto-allow — and with nothing auto-allowed there is no allowlist to trick (spec §4.2).
    if auto_allow == AUTO_ALLOW_NONE:
        return PolicyResult(
            Decision.APPROVAL,
            "this profile does not run generic commands without approval",
            argv=argv,
        )

    # 5. Shell operators cannot run under shell=False (defense-in-depth, not the main boundary).
    if _SHELL_META.search(normalized):
        return PolicyResult(
            Decision.APPROVAL,
            "shell operators require explicit approval",
            argv=argv,
            needs_shell=True,
        )

    exe = _basename(argv[0])

    # 6. Shell interpreters and general-purpose runtimes are complete execution environments. They
    #    are never "known-safe" (spec §2.9), whatever their arguments look like.
    if exe in _SHELL_INTERPRETERS:
        return PolicyResult(
            Decision.APPROVAL, f"{exe} is a shell interpreter and requires approval", argv=argv
        )
    if exe in _GENERAL_RUNTIMES:
        return PolicyResult(
            Decision.APPROVAL,
            f"{exe} is a general-purpose runtime (unrestricted file and network access) and "
            "requires approval",
            argv=argv,
        )
    # 6b. Wrappers exist to run *another* program, so screening their name screens nothing:
    #     ``env python -c …`` and ``find … -exec python …`` were both auto-allowed before v0.1.4.
    if exe in WRAPPER_EXECUTABLES:
        return PolicyResult(Decision.APPROVAL, WRAPPER_EXECUTABLES[exe], argv=argv)

    # 7. git config / git -c mutate or inject configuration, including pagers and hooks that execute.
    if _GIT_CONFIG.match(normalized):
        return PolicyResult(
            Decision.APPROVAL,
            "git configuration changes require approval",
            argv=argv,
        )

    # 8. Network use when the profile forbids it.
    if not network_allowed:
        for pattern in _NETWORK_PATTERNS:
            if pattern.search(normalized):
                return PolicyResult(
                    Decision.APPROVAL,
                    "network-oriented command requires approval for this profile",
                    argv=argv,
                )

    # 9. Destructive but sometimes-legitimate verbs.
    for pattern, reason in _APPROVAL_PATTERNS:
        if pattern.search(normalized):
            return PolicyResult(Decision.APPROVAL, reason, argv=argv)

    # 10. The narrow auto-allow sets. Everything else needs a human.
    #     Inspection is decided by an explicit per-executable validator that must account for every
    #     option it accepts — not by membership in a name list (spec §4.2).
    inspection = _validate_inspection(exe, argv, clamp=clamp, workspace_root=workspace_root)
    if inspection is not None:
        return inspection
    if purpose is Purpose.TEST and _in_table(exe, argv, _TEST_RUNNERS):
        return PolicyResult(Decision.ALLOW, argv=argv)
    if auto_allow == AUTO_ALLOW_BUILD and (
        _in_table(exe, argv, _TEST_RUNNERS) or _in_table(exe, argv, _BUILD_TOOLS)
    ):
        return PolicyResult(Decision.ALLOW, argv=argv)

    if purpose is Purpose.TEST:
        return PolicyResult(
            Decision.APPROVAL,
            f"{exe!r} is not one of the structured test/build runners; running it requires approval",
            argv=argv,
        )
    return PolicyResult(
        Decision.APPROVAL,
        f"{exe!r} is not a read-only inspection command; running it requires approval",
        argv=argv,
    )


def evaluate_test_argv(
    argv: tuple[str, ...], *, workspace_root: Path, profile: PermissionProfile
) -> PolicyResult:
    """Grant unattended test authority only to exact structured runner shapes."""

    if not argv or any(not isinstance(token, str) or not token or "\0" in token for token in argv):
        return PolicyResult(Decision.DENY, "test argv contains an invalid token")
    if any(_SHELL_META.search(token) for token in argv):
        return PolicyResult(
            Decision.APPROVAL,
            "shell operators or chaining are not part of structured test authority",
            argv=argv,
        )
    for token in argv[1:]:
        if profile.restrict_to_workspace and escapes_workspace(token, workspace_root):
            return PolicyResult(
                Decision.DENY, f"{token!r} resolves outside the workspace", argv=argv
            )

    executable = _basename(argv[0])
    allowed = False
    if executable == "pytest":
        allowed = True
    elif executable in {"python", "python3", "py"}:
        allowed = len(argv) >= 3 and argv[1:3] == ("-m", "pytest")
    elif executable in {"npm", "pnpm", "yarn"}:
        allowed = len(argv) >= 2 and (
            argv[1] == "test" or (len(argv) >= 3 and argv[1:3] == ("run", "test"))
        )
    elif executable in {"cargo", "go", "dotnet"}:
        allowed = len(argv) >= 2 and argv[1] == "test"
    if allowed:
        return PolicyResult(Decision.ALLOW, argv=argv)
    return PolicyResult(
        Decision.APPROVAL,
        "argv is not an approved structured test runner shape",
        argv=argv,
    )
