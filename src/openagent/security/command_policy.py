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

The model now:

* **Auto-allow is a narrow, purpose-scoped list**, not "everything known". Under a guarded profile
  only read-only *inspection* of the workspace runs unattended (``git status``, ``ls``, ``grep`` …).
* **Anything that executes code** — interpreters, package/build scripts, test runners via
  ``run_command`` — requires an explicit approval. ``run_tests`` has its own structured, bounded
  runner list (spec §2.3), because "run the project's tests" inherently executes project code and
  should be an explicit, named capability rather than a side effect of a generic command.
* **Arguments that resolve outside the workspace are rejected outright** (spec §2.6), so an
  allowlisted tool cannot be used as a file-exfiltration primitive.
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

#: Read-only inspection. These do not execute project or user code, and every path argument is
#: clamped to the workspace before they are allowed. ``None`` = any subcommand; a set = only those.
#: NOTE: ``git`` is restricted to genuinely read-only plumbing — `config` mutates global state and
#: `-c` injects config (pagers/hooks) that can execute, so both fall through to approval.
_INSPECT: dict[str, frozenset[str] | None] = {
    "ls": None,
    "pwd": None,
    "echo": None,
    "printf": None,
    "cat": None,
    "head": None,
    "tail": None,
    "wc": None,
    "sort": None,
    "uniq": None,
    "cut": None,
    "grep": None,
    "egrep": None,
    "fgrep": None,
    "rg": None,
    "ag": None,
    "find": None,
    "fd": None,
    "tree": None,
    "stat": None,
    "file": None,
    "diff": None,
    "cmp": None,
    "date": None,
    "which": None,
    "basename": None,
    "dirname": None,
    "realpath": None,
    "readlink": None,
    "true": None,
    "false": None,
    "test": None,
    "env": None,
    "printenv": None,
    "git": frozenset(
        {
            "status",
            "diff",
            "log",
            "show",
            "branch",
            "rev-parse",
            "ls-files",
            "describe",
            "blame",
            "shortlog",
            "tag",
        }
    ),
    "hg": frozenset({"status", "diff", "log"}),
}

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
    "sed": None,
    "awk": None,
    "tr": None,
    "tee": None,
    "xargs": None,
}

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

    Both the shlex-split argv **and** a raw whitespace split are checked, because POSIX shlex treats
    ``\\`` as an escape character: on Linux/macOS it silently rewrites ``C:\\Windows\\System32`` into
    ``C:WindowsSystem32``, which no longer looks absolute and would sail straight past the clamp. The
    raw split preserves the backslashes, so a Windows-style escape is caught on every platform.
    """

    tokens = list(argv[1:])
    for raw in command.split()[1:]:
        stripped = raw.strip("\"'")
        if stripped:
            tokens.append(stripped)
    return tokens


def _first_subcommand(argv: tuple[str, ...]) -> str | None:
    for token in argv[1:]:
        if not token.startswith("-"):
            return token
    return None


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

    # 3. No command execution at all for this profile.
    if auto_allow == AUTO_ALLOW_NONE:
        return PolicyResult(
            Decision.APPROVAL,
            "this profile does not run commands without approval",
            argv=argv,
        )

    # 4. Out-of-workspace arguments are rejected outright — an allowlisted tool must not become a
    #    read/write primitive for the rest of the filesystem (spec §2.6).
    if clamp and workspace_root is not None:
        for token in _path_candidates(normalized, argv):
            if escapes_workspace(token, workspace_root):
                return PolicyResult(
                    Decision.DENY,
                    f"{token!r} resolves outside the workspace; this profile confines commands to "
                    f"{workspace_root}",
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
    if _in_table(exe, argv, _INSPECT):
        return PolicyResult(Decision.ALLOW, argv=argv)
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
