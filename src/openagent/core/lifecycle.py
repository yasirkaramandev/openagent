"""Single source of truth for persisted run lifecycle transitions.

UI labels, cancellation, orphan recovery and repositories must agree about terminal/resumable state.
Keeping the graph here prevents one path from reviving a cancelled/orphaned run or racing another
process into a second terminal outcome.
"""

from __future__ import annotations

from collections.abc import Collection

from .models import RunStatus


class InvalidTransition(ValueError):
    """A requested run transition is not part of the lifecycle graph."""


TERMINAL = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.ORPHANED}
)

_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    # A queued row is persisted before its worker starts. If the process dies in that window, a
    # restarted instance has no queue/worker ownership to recover and must terminalize it as an
    # orphan instead of leaving an unexecutable run queued forever.
    RunStatus.QUEUED: frozenset(
        {RunStatus.STARTING, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.ORPHANED}
    ),
    RunStatus.STARTING: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.WAITING_APPROVAL,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.ORPHANED,
        }
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_APPROVAL,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.ORPHANED,
        }
    ),
    RunStatus.WAITING_APPROVAL: frozenset(
        {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.ORPHANED}
    ),
    # A completed/failed CLI session may take a deliberate follow-up turn. Cancelled and orphaned
    # runs may only be rerun under a new run id; they never transition back to active.
    RunStatus.COMPLETED: frozenset({RunStatus.RUNNING}),
    RunStatus.FAILED: frozenset({RunStatus.RUNNING}),
    RunStatus.CANCELLED: frozenset(),
    # An orphan that still has an identity-verified live backend may be explicitly terminated by
    # the user; that terminal-to-terminal transition records the actual disposition.
    RunStatus.ORPHANED: frozenset({RunStatus.CANCELLED}),
}


def _status(value: RunStatus | str) -> RunStatus:
    return value if isinstance(value, RunStatus) else RunStatus(value)


def is_terminal(status: RunStatus | str) -> bool:
    return _status(status) in TERMINAL


def can_resume(status: RunStatus | str) -> bool:
    return _status(status) in {RunStatus.COMPLETED, RunStatus.FAILED}


def can_cancel(status: RunStatus | str) -> bool:
    return _status(status) in {
        RunStatus.QUEUED,
        RunStatus.STARTING,
        RunStatus.RUNNING,
        RunStatus.WAITING_APPROVAL,
    }


def can_transition(current: RunStatus | str, target: RunStatus | str) -> bool:
    source, destination = _status(current), _status(target)
    return source == destination or destination in _TRANSITIONS[source]


def validate_transition(current: RunStatus | str, target: RunStatus | str) -> None:
    source, destination = _status(current), _status(target)
    if not can_transition(source, destination):
        raise InvalidTransition(f"invalid run transition: {source.value} -> {destination.value}")


def validate_expected(expected: Collection[RunStatus | str], target: RunStatus | str) -> None:
    """Validate every state accepted by a repository compare-and-set."""

    if not expected:
        raise InvalidTransition("a compare-and-set transition needs at least one expected state")
    for source in expected:
        validate_transition(source, target)
