"""ask_user returns the interactive answer and redacts secrets in the event stream (item 16)."""

from __future__ import annotations

from pathlib import Path

from openagent.core.events import EventType, NormalizedEvent
from openagent.core.permissions import SAFE_EDIT, get_profile
from openagent.security.approvals import ApprovalGate
from openagent.storage.event_log import EventLog
from openagent.tools.base import ToolContext
from openagent.tools.control import ask_user


def _ctx(root: Path, *, resolver=None, emit=None) -> ToolContext:
    return ToolContext(
        workspace_root=root, profile=get_profile(SAFE_EDIT),
        approval_gate=ApprovalGate(auto_approve=False), run_id="run_test",
        emit=emit, ask_user_callback=resolver,
    )


def _record_emit(events: list[tuple[str, dict]]):
    def emit(name: str, data: dict) -> None:
        events.append((name, data))
    return emit


def test_answer_returned_as_tool_result(tmp_path: Path):
    ctx = _ctx(tmp_path, resolver=lambda q: "use port 8080")
    result = ask_user(ctx, "which port?")
    assert result.ok
    assert result.content == "use port 8080"
    assert result.data.get("answered") is True


def test_emits_question_requested_then_answered(tmp_path: Path):
    events: list[tuple[str, dict]] = []
    ctx = _ctx(tmp_path, resolver=lambda q: "8080", emit=_record_emit(events))
    ask_user(ctx, "which port?")
    names = [n for n, _ in events]
    assert names == ["question.requested", "question.answered"]
    # Never an approval event for a plain question (item 13).
    assert not any(n.startswith("approval.") for n in names)


def test_emits_question_cancelled_when_no_resolver(tmp_path: Path):
    events: list[tuple[str, dict]] = []
    ctx = _ctx(tmp_path, emit=_record_emit(events))  # no resolver -> non-interactive
    ask_user(ctx, "which port?")
    names = [n for n, _ in events]
    assert names == ["question.requested", "question.cancelled"]
    assert events[-1][1]["reason"] == "no interactive user available"


def test_emits_question_cancelled_when_user_cancels(tmp_path: Path):
    events: list[tuple[str, dict]] = []
    ctx = _ctx(tmp_path, resolver=lambda q: None, emit=_record_emit(events))
    ask_user(ctx, "which port?")
    assert [n for n, _ in events] == ["question.requested", "question.cancelled"]
    assert events[-1][1]["reason"] == "cancelled"


def test_no_resolver_falls_back_to_best_judgment(tmp_path: Path):
    result = ask_user(_ctx(tmp_path), "which port?")
    assert result.ok
    assert "best judgment" in result.content
    assert result.data.get("answered") is False


def test_cancelled_answer_falls_back(tmp_path: Path):
    ctx = _ctx(tmp_path, resolver=lambda q: None)
    result = ask_user(ctx, "which port?")
    assert result.data.get("answered") is False


def test_blank_answer_falls_back(tmp_path: Path):
    ctx = _ctx(tmp_path, resolver=lambda q: "   ")
    result = ask_user(ctx, "which port?")
    assert result.data.get("answered") is False


def test_question_and_answer_recorded_and_secret_redacted(tmp_path: Path):
    # Wire ctx.emit through a real EventLog so redaction is exercised on the recorded answer.
    log = EventLog(tmp_path / "run")

    def emit(name: str, data: dict) -> None:
        try:
            etype = EventType(name)
        except ValueError:
            etype = EventType.LOG
        log.append(NormalizedEvent(run_id="run_test", type=etype, source="api-agent", data=data))

    ctx = _ctx(tmp_path, resolver=lambda q: "the key is sk-ABCDEF1234567890abcdef", emit=emit)
    result = ask_user(ctx, "what is the key?")
    # The model still receives the true answer as the tool result…
    assert "sk-ABCDEF1234567890abcdef" in result.content
    # …but the persisted event stream has it redacted.
    events_text = (tmp_path / "run" / "events.jsonl").read_text()
    assert "sk-ABCDEF1234567890abcdef" not in events_text
    assert "question.answered" in events_text
    assert "what is the key?" in events_text
