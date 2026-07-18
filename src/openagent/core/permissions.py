"""Permission profiles (spec §29).

A profile controls what an agent may do: which tools are exposed to an API agent, whether the
network/edits are allowed, and how each profile maps onto a CLI's own sandbox/permission flags
(spec §7 for Codex, §8 for Claude).
"""

from __future__ import annotations

from dataclasses import dataclass, field

READ_ONLY = "read-only"
SAFE_EDIT = "safe-edit"
DEVELOPMENT = "development"
FULL_ACCESS = "full-access"


#: How much command execution runs **without** an explicit human approval (spec §2).
#:
#: This is a *policy* tier, not an OS sandbox — see SECURITY.md. It exists because the pre-v0.1.3
#: design auto-approved every executable on a broad allowlist (``python``, ``node``, ``cat``, ``git``…),
#: which handed out arbitrary code execution and host file access for free.
AUTO_ALLOW_NONE = "none"  # no commands at all
AUTO_ALLOW_INSPECT = (
    "inspect"  # read-only inspection inside the workspace; everything else approves
)
AUTO_ALLOW_BUILD = "build"  # inspect + test/build/package tooling
AUTO_ALLOW_ALL = "all"  # anything the denylist does not forbid (explicit user consent)


@dataclass(frozen=True)
class PermissionProfile:
    name: str
    description: str
    #: OpenAgent tool names an API agent may call (see tools/registry.py).
    allowed_tools: frozenset[str]
    can_edit_files: bool
    can_run_commands: bool
    network_allowed: bool
    require_approval_for_destructive: bool
    #: Codex ``--sandbox`` value (spec §7).
    codex_sandbox: str
    #: Claude ``--permission-mode`` value, or None (spec §8).
    claude_permission_mode: str | None
    #: Claude ``--allowedTools`` list (spec §8).
    claude_allowed_tools: tuple[str, ...] = field(default_factory=tuple)
    #: Which commands may run with no approval (see AUTO_ALLOW_* above).
    command_auto_allow: str = AUTO_ALLOW_INSPECT
    #: Reject arguments that resolve outside the workspace outright (spec §2.6).
    restrict_to_workspace: bool = True
    #: May ``run_tests`` execute the project's own code **on the host** with no approval (spec §3)?
    #: Defaults to False because that is arbitrary code execution: the agent writes the test files.
    #: Only profiles whose whole point is accepting host risk set this. Under a container-sandbox
    #: backend the question does not arise — the sandbox is the containment.
    auto_run_project_code_on_host: bool = False
    #: A short, honest statement of what this profile does NOT protect against (spec §2.5, §2.10).
    risk_note: str = ""


#: ``update_plan`` / ``report_progress`` publish user-visible progress (item 12). They touch nothing,
#: so every profile — including read-only — exposes them: transparency is never a privilege.
_READ_TOOLS = frozenset(
    {
        "list_files",
        "read_file",
        "search_files",
        "search_text",
        "git_status",
        "git_diff",
        "ask_user",
        "update_plan",
        "report_progress",
        "finish_task",
    }
)
_EDIT_TOOLS = _READ_TOOLS | {"write_file", "apply_patch"}
_FULL_TOOLS = _EDIT_TOOLS | {"run_command", "run_tests"}


PROFILES: dict[str, PermissionProfile] = {
    READ_ONLY: PermissionProfile(
        name=READ_ONLY,
        description="Read and search only. No edits, no command execution.",
        allowed_tools=_READ_TOOLS,
        can_edit_files=False,
        can_run_commands=False,
        network_allowed=False,
        require_approval_for_destructive=True,
        codex_sandbox="read-only",
        claude_permission_mode=None,
        claude_allowed_tools=("Read",),
        command_auto_allow=AUTO_ALLOW_NONE,
        restrict_to_workspace=True,
        risk_note="No command execution at all (spec §2.4).",
    ),
    SAFE_EDIT: PermissionProfile(
        name=SAFE_EDIT,
        description=(
            "Edit files in the workspace and run the project's tests. Only read-only inspection "
            "runs without approval; interpreters, package/build scripts and network need approval."
        ),
        allowed_tools=_FULL_TOOLS,
        can_edit_files=True,
        can_run_commands=True,
        network_allowed=False,
        require_approval_for_destructive=True,
        codex_sandbox="workspace-write",
        claude_permission_mode="acceptEdits",
        claude_allowed_tools=(
            "Read",
            "Edit",
            "Bash(git diff *)",
            "Bash(git status *)",
            "Bash(pytest *)",
            "Bash(npm test *)",
        ),
        # Nothing runs unattended through the generic ``run_command`` string (spec §4.2). Screening
        # an executable's *name* screens nothing — ``env python -c …``, ``find … -exec …`` and
        # ``sort --output=…`` all used to sail through an allowlist of "read-only" tools. The
        # inspection an agent genuinely needs is served by dedicated tools (read_file, search_text,
        # git_status, git_diff) whose arguments are structured and cannot name another program.
        command_auto_allow=AUTO_ALLOW_NONE,
        restrict_to_workspace=True,
        # The agent may write test files and conftest.py, so "run the tests" on the host is
        # arbitrary code execution. Under host-restricted it needs a human; under a container
        # sandbox it runs unattended because the sandbox — not this flag — is the boundary.
        auto_run_project_code_on_host=False,
        risk_note=(
            "POLICY boundary only — NOT an OS sandbox. Running the project's own tests executes "
            "that project's code, so on this host it requires your explicit approval each time; "
            "under container-sandbox it runs contained. Network is gated by policy, not by the "
            "kernel. Use a container/VM for untrusted code."
        ),
    ),
    DEVELOPMENT: PermissionProfile(
        name=DEVELOPMENT,
        description=(
            "Edit files, install packages, use network, run tests/build. Interpreters and "
            "destructive operations still need approval."
        ),
        allowed_tools=_FULL_TOOLS,
        can_edit_files=True,
        can_run_commands=True,
        network_allowed=True,
        require_approval_for_destructive=True,
        codex_sandbox="workspace-write",
        claude_permission_mode="acceptEdits",
        claude_allowed_tools=("Read", "Edit", "Bash"),
        command_auto_allow=AUTO_ALLOW_BUILD,
        restrict_to_workspace=True,
        # Explicitly riskier by design: this profile exists to run build/test tooling unattended.
        auto_run_project_code_on_host=True,
        risk_note=(
            "POLICY boundary only — NOT an OS sandbox. The project's tests and build scripts run "
            "on this host without asking. Package managers run arbitrary install "
            "scripts from the network with no kernel-level restriction. Use a container/VM for "
            "untrusted dependencies."
        ),
    ),
    FULL_ACCESS: PermissionProfile(
        name=FULL_ACCESS,
        description="Unrestricted. Requires explicit user consent; container/worktree strongly recommended.",
        allowed_tools=_FULL_TOOLS,
        can_edit_files=True,
        can_run_commands=True,
        network_allowed=True,
        require_approval_for_destructive=False,
        codex_sandbox="danger-full-access",
        claude_permission_mode="acceptEdits",
        claude_allowed_tools=("Read", "Edit", "Bash"),
        command_auto_allow=AUTO_ALLOW_ALL,
        restrict_to_workspace=False,
        # Choosing full-access *is* the explicit consent (spec §3.2).
        auto_run_project_code_on_host=True,
        risk_note=(
            "NO restrictions beyond the categorical denylist: the agent may read and write ANY file "
            "your user account can, and use the network freely. Only run this against code you "
            "trust, in a container or VM."
        ),
    ),
}


def get_profile(name: str) -> PermissionProfile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown permission profile {name!r}; choose from {sorted(PROFILES)}"
        ) from exc


def profile_names() -> list[str]:
    return list(PROFILES)
