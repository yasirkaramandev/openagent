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
        description="Read and search only. No edits, limited commands.",
        allowed_tools=_READ_TOOLS,
        can_edit_files=False,
        can_run_commands=False,
        network_allowed=False,
        require_approval_for_destructive=True,
        codex_sandbox="read-only",
        claude_permission_mode=None,
        claude_allowed_tools=("Read",),
    ),
    SAFE_EDIT: PermissionProfile(
        name=SAFE_EDIT,
        description="Edit files in the workspace, run tests/build. Network commands need approval; no push/publish.",
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
    ),
    DEVELOPMENT: PermissionProfile(
        name=DEVELOPMENT,
        description="Edit files, install packages, use network, run tests/build. Approval for destructive ops.",
        allowed_tools=_FULL_TOOLS,
        can_edit_files=True,
        can_run_commands=True,
        network_allowed=True,
        require_approval_for_destructive=True,
        codex_sandbox="workspace-write",
        claude_permission_mode="acceptEdits",
        claude_allowed_tools=("Read", "Edit", "Bash"),
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
