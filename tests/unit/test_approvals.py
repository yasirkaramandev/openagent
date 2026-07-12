"""Approval gate: emits approval events and honors policy/callback (spec §29)."""

from __future__ import annotations

from openagent.security.approvals import ApprovalGate, ApprovalOutcome, ApprovalRequest


def _req() -> ApprovalRequest:
    return ApprovalRequest(run_id="r", action="run_command", detail="rm -rf build",
                           command="rm -rf build", reason="recursive delete", workspace="/ws")


def test_non_interactive_defaults_to_deny():
    events: list[tuple[str, dict]] = []
    gate = ApprovalGate(auto_approve=False, emit=lambda n, d: events.append((n, d)), run_id="r")
    assert gate.decide(_req()) is ApprovalOutcome.DENIED
    names = [n for n, _ in events]
    assert names == ["approval.requested", "approval.denied"]


def test_auto_approve_accepts_and_emits():
    events: list[tuple[str, dict]] = []
    gate = ApprovalGate(auto_approve=True, emit=lambda n, d: events.append((n, d)))
    assert gate.decide(_req()) is ApprovalOutcome.ACCEPTED
    assert [n for n, _ in events] == ["approval.requested", "approval.accepted"]


def test_callback_wins_over_auto_policy():
    seen: list[ApprovalRequest] = []

    def approve(req: ApprovalRequest) -> bool:
        seen.append(req)
        return True

    gate = ApprovalGate(auto_approve=False, callback=approve)
    assert gate.decide(_req()) is ApprovalOutcome.ACCEPTED
    assert seen and seen[0].command == "rm -rf build"


def test_callback_denial():
    gate = ApprovalGate(callback=lambda req: False)
    assert gate.decide(_req()) is ApprovalOutcome.DENIED


def test_emitted_event_carries_context():
    events: list[tuple[str, dict]] = []
    gate = ApprovalGate(auto_approve=True, emit=lambda n, d: events.append((n, d)))
    gate.decide(_req())
    _, data = events[0]
    assert data["command"] == "rm -rf build"
    assert data["reason"] == "recursive delete"
    assert data["workspace"] == "/ws"
