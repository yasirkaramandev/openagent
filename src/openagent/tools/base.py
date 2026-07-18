"""Tool context, result, and path-safety helpers shared by all tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.cancellation import RunCancellation
from ..core.permissions import PermissionProfile
from ..security.approvals import ApprovalGate, ApprovalRequest
from ..security.filesystem import SafeWorkspaceWalker, UnsafeWorkspacePath

if TYPE_CHECKING:
    from ..security.execution_backend import ExecutionBackend


class ToolError(Exception):
    """Raised by tools for expected, reportable failures (bad path, denied command…)."""


class ToolExecutionInternalError(RuntimeError):
    """A tool handler violated an internal invariant; safe to expose, with no raw exception text."""

    error_type = "tool_internal_error"

    def __init__(self, tool_name: str) -> None:
        super().__init__(f"tool {tool_name!r} failed due to an internal error")


@dataclass
class ToolResult:
    ok: bool
    content: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, content: str = "", **data: Any) -> ToolResult:
        return cls(ok=True, content=content, data=data)

    @classmethod
    def failure(cls, content: str, **data: Any) -> ToolResult:
        return cls(ok=False, content=content, data=data)


#: Signature for the optional event sink a runtime passes so tools can emit fine-grained events.
EventSink = Callable[[str, dict], None]

#: Resolver for ``ask_user``: given a question, return the user's answer, or ``None`` when no
#: interactive user is available or they cancelled (the tool then falls back to best judgment).
AskUserResolver = Callable[[str], "str | None"]


@dataclass
class ToolContext:
    """Everything a tool needs, scoped to one run."""

    workspace_root: Path
    profile: PermissionProfile
    approval_gate: ApprovalGate
    run_id: str = ""
    emit: EventSink | None = None
    #: Resolver that answers ``ask_user`` from a real interactive user (TUI modal). ``None`` in a
    #: non-interactive run, where ``ask_user`` falls back to best-judgment (item 16).
    ask_user_callback: AskUserResolver | None = None
    #: Extra environment variables injected into command subprocesses for *this* run only. Empty by
    #: default: an API agent's commands never inherit provider keys or the parent environment
    #: (spec §7). Populate only with credentials a specific operation explicitly needs.
    command_env: dict[str, str] = field(default_factory=dict)
    #: The run's cancellation controller (item 9.2). A blocking tool subprocess (``run_command`` /
    #: ``run_tests``) polls it so a Cancel kills the whole process tree *while the command is still
    #: running* — not only after it exits. ``None`` in contexts without a live run (unit tests).
    cancellation: RunCancellation | None = None
    execution_backend: ExecutionBackend | None = None

    def request_approval(
        self, action: str, detail: str, *, command: str = "", reason: str = ""
    ) -> bool:
        request = ApprovalRequest(
            run_id=self.run_id,
            action=action,
            detail=detail,
            command=command or detail,
            reason=reason,
            workspace=str(self.workspace_root),
        )
        return self.approval_gate.decide(request).value == "accepted"

    def resolve_path(self, relative: str) -> Path:
        """Resolve ``relative`` inside the workspace, rejecting escapes and symlink breakouts.

        Prevents path traversal (``../../etc/passwd``) and absolute-path escapes (spec §40 security
        tests). Returns an absolute path guaranteed to live under ``workspace_root``.
        """

        try:
            return self.walker().resolve(relative, allow_missing=True)
        except (UnsafeWorkspacePath, OSError) as exc:
            raise ToolError(f"unsafe workspace path {relative!r}: {exc}") from exc

    def walker(self) -> SafeWorkspaceWalker:
        return SafeWorkspaceWalker(self.workspace_root, cancellation=self.cancellation)
