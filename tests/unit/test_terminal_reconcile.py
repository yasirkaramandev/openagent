"""Fail-closed terminal reconciliation precedence: cancelled > failed > completed (item 8).

Exercises :func:`reconcile_terminal` directly so the full precedence matrix — including a native
``run.cancelled`` (which the Codex mapper never emits) — is covered, and asserts exactly one
normalized terminal event is produced for every combination.
"""

from __future__ import annotations

from openagent.core.events import EventType, NormalizedEvent
from openagent.runtimes.cli.base import TerminalObservations, reconcile_terminal


def _ev(t: EventType, **data: object) -> NormalizedEvent:
    return NormalizedEvent(run_id="run_x", type=t, source="test", data=data)


def _obs(*events: NormalizedEvent) -> TerminalObservations:
    obs = TerminalObservations()
    for e in events:
        obs.observe(e)
    return obs


def _final(obs: TerminalObservations, *, exit_code: int | None = 0, cancelled: bool = False) -> str:
    ev = reconcile_terminal(
        run_id="run_x", source="test", observations=obs, exit_code=exit_code, cancelled=cancelled
    )
    t = ev.type if isinstance(ev.type, str) else ev.type.value
    return t


COMPLETED = EventType.RUN_COMPLETED.value
FAILED = EventType.RUN_FAILED.value
CANCELLED = EventType.RUN_CANCELLED.value


def test_completed_then_failed_exit0_is_failed() -> None:
    obs = _obs(_ev(EventType.RUN_COMPLETED), _ev(EventType.RUN_FAILED))
    assert _final(obs, exit_code=0) == FAILED


def test_failed_then_completed_exit0_is_failed() -> None:
    obs = _obs(_ev(EventType.RUN_FAILED), _ev(EventType.RUN_COMPLETED))
    assert _final(obs, exit_code=0) == FAILED


def test_completed_then_completed_exit0_is_completed_once() -> None:
    obs = _obs(_ev(EventType.RUN_COMPLETED), _ev(EventType.RUN_COMPLETED))
    ev = reconcile_terminal(
        run_id="run_x", source="test", observations=obs, exit_code=0, cancelled=False
    )
    assert (ev.type if isinstance(ev.type, str) else ev.type.value) == COMPLETED


def test_cancelled_then_completed_is_cancelled() -> None:
    obs = _obs(_ev(EventType.RUN_CANCELLED), _ev(EventType.RUN_COMPLETED))
    assert _final(obs, exit_code=0) == CANCELLED


def test_completed_with_nonzero_exit_is_failed() -> None:
    obs = _obs(_ev(EventType.RUN_COMPLETED))
    ev = reconcile_terminal(
        run_id="run_x", source="test", observations=obs, exit_code=1, cancelled=False
    )
    assert (ev.type if isinstance(ev.type, str) else ev.type.value) == FAILED
    assert ev.data.get("error_type") == "exit_code_mismatch"


def test_no_event_exit0_is_failed() -> None:
    assert _final(_obs(), exit_code=0) == FAILED


def test_no_event_exit0_reports_no_terminal_event() -> None:
    ev = reconcile_terminal(
        run_id="run_x", source="test", observations=_obs(), exit_code=0, cancelled=False
    )
    assert ev.data.get("error_type") == "no_terminal_event"


def test_failed_event_exit0_is_failed() -> None:
    assert _final(_obs(_ev(EventType.RUN_FAILED)), exit_code=0) == FAILED


def test_completed_exit0_is_completed() -> None:
    assert _final(_obs(_ev(EventType.RUN_COMPLETED)), exit_code=0) == COMPLETED


def test_explicit_process_cancel_wins_over_completed() -> None:
    # A killed process that also streamed a completion is cancelled, never completed.
    obs = _obs(_ev(EventType.RUN_COMPLETED))
    assert _final(obs, exit_code=0, cancelled=True) == CANCELLED


def test_conflict_is_flagged_terminal_conflict() -> None:
    obs = _obs(_ev(EventType.RUN_COMPLETED), _ev(EventType.RUN_FAILED))
    ev = reconcile_terminal(
        run_id="run_x", source="test", observations=obs, exit_code=0, cancelled=False
    )
    assert ev.data.get("error_type") == "terminal_conflict"
