"""Approval flow (spec §29).

When the command policy returns ``APPROVAL`` (a destructive verb, an off-allowlist executable, a
shell-operator command, network use under a no-network profile…), the runtime pauses and asks. An
:class:`ApprovalGate` decides how that question is answered and records the decision as events:

* ``approval.requested`` — a decision is needed;
* ``approval.accepted`` / ``approval.denied`` — the resolution.

Resolution order: an explicit ``resolver`` callback (a TUI modal / CLI confirm) if present, else the
``auto_approve`` policy. The default is **deny** — non-interactive runs never silently approve a
high-risk operation, and ``safe-edit`` (which requires approval for destructive ops) can never be
auto-approved.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


class ApprovalOutcome(str, Enum):
    ACCEPTED = "accepted"
    DENIED = "denied"


@dataclass
class ApprovalRequest:
    run_id: str
    action: str
    detail: str
    #: Optional structured context surfaced in the TUI modal.
    command: str = ""
    reason: str = ""
    workspace: str = ""


ApprovalCallback = Callable[[ApprovalRequest], bool]
#: Sink for approval.* events, matching tools' ``emit(name, data)`` signature.
ApprovalEmit = Callable[[str, dict], None]


class ApprovalGate:
    """Resolves approval requests according to a policy or a callback, emitting approval events."""

    def __init__(
        self,
        *,
        auto_approve: bool = False,
        callback: ApprovalCallback | None = None,
        emit: ApprovalEmit | None = None,
        run_id: str = "",
    ) -> None:
        self.auto_approve = auto_approve
        self.callback = callback
        self.emit = emit
        self.run_id = run_id

    def decide(self, request: ApprovalRequest) -> ApprovalOutcome:
        self._emit("approval.requested", request, extra={"auto": self.callback is None})
        if self.callback is not None:
            accepted = bool(self.callback(request))
        else:
            accepted = self.auto_approve
        outcome = ApprovalOutcome.ACCEPTED if accepted else ApprovalOutcome.DENIED
        self._emit(f"approval.{outcome.value}", request)
        return outcome

    def _emit(self, name: str, request: ApprovalRequest, extra: dict | None = None) -> None:
        if self.emit is None:
            return
        data = {
            "action": request.action,
            "detail": request.detail,
            "command": request.command or request.detail,
            "reason": request.reason,
            "workspace": request.workspace,
        }
        if extra:
            data.update(extra)
        self.emit(name, data)
