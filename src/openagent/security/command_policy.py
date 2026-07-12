"""Command policy (spec §29).

Before any shell command runs (whether requested by an API agent's ``run_command`` tool or emitted
by a CLI runtime we control), it is screened here. The **primary** boundary is an executable
*allowlist* combined with a minimal subprocess environment and ``shell=False`` execution — not a
regex denylist. The denylist and approval patterns are defense-in-depth on top of that.

Outcomes beyond "allow":

* ``DENY`` — categorically forbidden (push, publish, sudo, credential reads…). Never runs.
* ``APPROVAL`` — high-risk but sometimes legitimate; requires an explicit approval first. This
  covers destructive verbs, network use when the profile forbids it, shell-operator commands, shell
  interpreters, and any executable not on the allowlist.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from enum import Enum


class Decision(str, Enum):
    ALLOW = "allow"
    APPROVAL = "approval"
    DENY = "deny"


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


# --------------------------------------------------------------------------- allowlist (primary)

#: Executables an agent may run directly. Matched against the basename of ``argv[0]``. Anything not
#: here requires explicit approval — the allowlist, not the denylist, is the boundary.
_ALLOWED_EXECUTABLES = frozenset({
    # language runtimes / package managers
    "python", "python3", "py", "pip", "pip3", "pipx", "uv", "poetry", "pytest",
    "node", "npm", "npx", "yarn", "pnpm", "bun", "deno", "tsc",
    "go", "cargo", "rustc", "java", "javac", "mvn", "gradle", "gradlew",
    "ruby", "gem", "bundle", "rake", "php", "composer", "dotnet",
    # build / test / lint / format
    "make", "cmake", "ninja", "meson", "tox", "nox", "ruff", "mypy", "black",
    "isort", "flake8", "pylint", "eslint", "prettier", "jest", "mocha", "vitest",
    # version control (dangerous git verbs are handled by the denylist / approval patterns)
    "git", "hg",
    # common read/inspect unix tools
    "ls", "cat", "echo", "printf", "pwd", "head", "tail", "wc", "sort", "uniq",
    "cut", "grep", "egrep", "fgrep", "rg", "ag", "find", "fd", "tree", "stat",
    "file", "diff", "cmp", "env", "printenv", "true", "false", "date", "sleep",
    "which", "basename", "dirname", "realpath", "readlink",
    # scoped file ops (destructive variants caught by approval/deny patterns)
    "mkdir", "touch", "cp", "mv", "ln", "sed", "awk", "tr", "tee", "xargs", "test",
})

#: Shell interpreters. Allowing them defeats ``shell=False`` argv sandboxing, so they are treated as
#: high-risk and require approval even though they are "known" binaries.
_SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "pwsh"})

#: Characters/tokens that force a real shell (pipes, redirects, subshells, chaining).
_SHELL_META = re.compile(r"[|&;<>`]|\$\(|\$\{|>>|\|\||&&|\n")


# --------------------------------------------------------------------------- denylist (secondary)

#: Categorically denied (spec §29 "Varsayılan yasaklar").
_DENY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bgit\s+push\b"), "git push is not allowed"),
    (re.compile(r"\bnpm\s+publish\b"), "npm publish is not allowed"),
    (re.compile(r"\b(pip|twine)\s+upload\b"), "package upload is not allowed"),
    (re.compile(r"\bdocker\s+login\b"), "docker login is not allowed"),
    (re.compile(r"\b(aws|gcloud|az)\s+.*\blogin\b"), "cloud CLI login is not allowed"),
    (re.compile(r"\bsudo\b"), "sudo is not allowed"),
    (re.compile(r"\bsecurity\s+find-generic-password\b"), "keychain access is not allowed"),
    (re.compile(r"(?i)\b(cat|less|more|head|tail|bat)\b[^\n|;&]*\.env\b"), "reading .env is not allowed"),
    (re.compile(r"(?i)id_rsa|id_ed25519|\.ssh/"), "SSH private key access is not allowed"),
    (re.compile(r"(?i)\b(cat|less|more)\b[^\n|;&]*(credentials|\.aws/)"), "reading credentials is not allowed"),
]

# High-risk: allowed only after approval.
_APPROVAL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+-[rfRF]"), "recursive/forced delete"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "hard reset discards changes"),
    (re.compile(r"\bgit\s+clean\b"), "git clean removes untracked files"),
    (re.compile(r"\b(mkfs|dd)\b"), "disk-level operation"),
    (re.compile(r"\bchmod\s+-R\b"), "recursive permission change"),
    (re.compile(r">\s*/dev/sd"), "raw device write"),
]

# Commands that need network (blocked when the profile disallows network).
_NETWORK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(curl|wget|nc|ncat|telnet)\b"),
    re.compile(r"\b(pip|pip3)\s+install\b"),
    re.compile(r"\b(npm|pnpm|yarn)\s+(install|add|ci)\b"),
    re.compile(r"\b(apt|apt-get|brew|dnf|yum)\s+install\b"),
    re.compile(r"\bgit\s+(clone|fetch|pull)\b"),
]


def split_command(command: str) -> list[str]:
    """Best-effort tokenization for inspection/logging."""

    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _basename(executable: str) -> str:
    return executable.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def evaluate(command: str, *, network_allowed: bool = False) -> PolicyResult:
    """Screen a raw shell command string and return a structured decision."""

    normalized = command.strip()
    if not normalized:
        return PolicyResult(Decision.DENY, "empty command")

    # 1. Categorical denials always win.
    for pattern, reason in _DENY_PATTERNS:
        if pattern.search(normalized):
            return PolicyResult(Decision.DENY, reason)

    argv = tuple(split_command(normalized))

    # 2. Shell operators can't run under shell=False; gate them behind approval.
    if _SHELL_META.search(normalized) or not argv:
        return PolicyResult(
            Decision.APPROVAL, "shell operators require explicit approval",
            argv=argv, needs_shell=True,
        )

    # 3. Network use when the profile forbids it.
    if not network_allowed:
        for pattern in _NETWORK_PATTERNS:
            if pattern.search(normalized):
                return PolicyResult(
                    Decision.APPROVAL, "network access is disabled for this profile", argv=argv,
                )

    # 4. Destructive but sometimes-legitimate verbs.
    for pattern, reason in _APPROVAL_PATTERNS:
        if pattern.search(normalized):
            return PolicyResult(Decision.APPROVAL, reason, argv=argv)

    exe = _basename(argv[0])
    # 5. Shell interpreters escape argv sandboxing → high-risk.
    if exe in _SHELL_INTERPRETERS:
        return PolicyResult(
            Decision.APPROVAL, f"{exe} is a shell interpreter and requires approval", argv=argv,
        )
    # 6. The allowlist is the primary boundary: unknown executables need approval.
    if exe not in _ALLOWED_EXECUTABLES:
        return PolicyResult(
            Decision.APPROVAL, f"{exe!r} is not on the executable allowlist", argv=argv,
        )

    return PolicyResult(Decision.ALLOW, argv=argv)


def is_allowed_executable(executable: str) -> bool:
    """Whether ``executable`` (path or name) is on the allowlist."""

    return _basename(executable) in _ALLOWED_EXECUTABLES
