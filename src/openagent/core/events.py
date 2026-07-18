"""Normalized events (spec §6.3).

Every runtime — an API tool-loop or a CLI subprocess — emits :class:`NormalizedEvent`s with this
exact shape and vocabulary. That uniformity is what lets the TUI, CLI, and artifact writers stay
runtime-agnostic.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventType(str, Enum):
    # run lifecycle — exactly one run.started and one terminal event per run (spec §6.3, item 4)
    RUN_STARTED = "run.started"
    RUN_PHASE = "run.phase"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    #: The run's owning process vanished without recording an outcome (spec §7.3). Distinct from
    #: ``run.failed``: the run did not fail, we *lost track of it*, and the two need different
    #: recovery and different words in the UI. Folding it into run.failed erased that distinction.
    RUN_ORPHANED = "run.orphaned"
    #: A backend subprocess was launched (carries pid/create_time). Distinct from ``run.started``,
    #: which OpenAgent alone owns — a CLI adapter must never claim to start the *run* (item 4).
    PROCESS_STARTED = "process.started"
    WORKSPACE_PREPARED = "workspace.prepared"
    # sessions
    SESSION_CREATED = "session.created"
    SESSION_RESUMED = "session.resumed"
    # assistant messages
    MESSAGE_STARTED = "message.started"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"
    #: A **user-visible reasoning summary** supplied by the backend (Codex ``reasoning`` items) or an
    #: explicit OpenAgent progress report. Never raw/hidden chain-of-thought (spec §6, item 1).
    REASONING_SUMMARY = "reasoning.summary"
    #: An explicit, user-facing progress statement from an OpenAgent-owned API agent (item 12).
    PROGRESS_UPDATED = "progress.updated"
    #: The agent's current checklist/plan (Codex ``todo_list``, or the ``update_plan`` tool).
    PLAN_UPDATED = "plan.updated"
    # tools
    TOOL_REQUESTED = "tool.requested"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    # shell commands
    COMMAND_STARTED = "command.started"
    COMMAND_OUTPUT = "command.output"
    COMMAND_COMPLETED = "command.completed"
    # web search
    WEB_SEARCH_STARTED = "web_search.started"
    WEB_SEARCH_COMPLETED = "web_search.completed"
    # file changes
    FILE_READ = "file.read"
    FILE_CREATED = "file.created"
    FILE_MODIFIED = "file.modified"
    FILE_DELETED = "file.deleted"
    # approvals (reserved for permission decisions only)
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_ACCEPTED = "approval.accepted"
    APPROVAL_DENIED = "approval.denied"
    # ask_user questions (distinct from approvals, spec §2.1)
    QUESTION_REQUESTED = "question.requested"
    QUESTION_ANSWERED = "question.answered"
    QUESTION_CANCELLED = "question.cancelled"
    # accounting / reliability
    USAGE_UPDATED = "usage.updated"
    RATE_LIMIT_DETECTED = "rate_limit.detected"
    RETRY_SCHEDULED = "retry.scheduled"
    # machine-level CLI updater audit events (not run lifecycle terminals)
    CLI_UPDATE_STARTED = "cli.update.started"
    CLI_UPDATE_COMPLETED = "cli.update.completed"
    CLI_UPDATE_FAILED = "cli.update.failed"
    CLI_UPDATE_RESTART_REQUIRED = "cli.update.restart_required"
    # artifacts
    ARTIFACT_CREATED = "artifact.created"
    TEST_COMPLETED = "test.completed"
    # anything an adapter can't map cleanly
    LOG = "log"


class ItemStatus(str, Enum):
    """Lifecycle status of an item-oriented event (spec §6.3, item 3).

    Item events (commands, files, tools, plans, searches, reasoning summaries) carry an ``item_id``
    and a ``status``. ``events.jsonl`` stays append-only — an update is a *new* event — while readers
    project the current state per ``(source, item_id)`` (see :mod:`openagent.core.projection`).
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunPhase(str, Enum):
    """Where a run currently is in its lifecycle — reported via ``run.phase`` (item 4)."""

    QUEUED = "queued"
    PREFLIGHT = "preflight"
    PREPARING_WORKSPACE = "preparing_workspace"
    STARTING_BACKEND = "starting_backend"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_USER = "waiting_user"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: The owning process vanished; see EventType.RUN_ORPHANED.
    ORPHANED = "orphaned"


#: Event types that describe one addressable item (projected by ``(source, item_id)``).
ITEM_EVENT_TYPES = frozenset(
    {
        EventType.REASONING_SUMMARY.value,
        EventType.PROGRESS_UPDATED.value,
        EventType.PLAN_UPDATED.value,
        EventType.COMMAND_STARTED.value,
        EventType.COMMAND_OUTPUT.value,
        EventType.COMMAND_COMPLETED.value,
        EventType.WEB_SEARCH_STARTED.value,
        EventType.WEB_SEARCH_COMPLETED.value,
        EventType.FILE_CREATED.value,
        EventType.FILE_MODIFIED.value,
        EventType.FILE_DELETED.value,
        EventType.TOOL_STARTED.value,
        EventType.TOOL_COMPLETED.value,
        EventType.TOOL_FAILED.value,
        EventType.MESSAGE_STARTED.value,
        EventType.MESSAGE_DELTA.value,
        EventType.MESSAGE_COMPLETED.value,
    }
)


def new_event_id() -> str:
    return "evt_" + uuid.uuid4().hex[:16]


class NormalizedEvent(BaseModel):
    """A single event in a run's ``events.jsonl`` (spec §6.3)."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    id: str = Field(default_factory=new_event_id)
    run_id: str
    timestamp: str = Field(default_factory=_now_iso)
    type: EventType
    source: str  # e.g. "codex-cli", "claude-cli", "api-agent", "openagent"
    data: dict[str, Any] = Field(default_factory=dict)

    def to_json_line(self) -> str:
        return self.model_dump_json()


#: The event types that end a run. Defined here, next to ``EventType``, because several layers need
#: the same answer — the event log (which must flush its export before the process may exit), the run
#: service, the projection and the artifact writer. When each kept its own copy, adding
#: ``run.orphaned`` meant finding all of them.
TERMINAL_EVENT_TYPES = frozenset(
    {
        EventType.RUN_COMPLETED.value,
        EventType.RUN_FAILED.value,
        EventType.RUN_CANCELLED.value,
        EventType.RUN_ORPHANED.value,
    }
)


def is_terminal_event_type(event_type: EventType | str) -> bool:
    return (event_type if isinstance(event_type, str) else event_type.value) in TERMINAL_EVENT_TYPES


# --------------------------------------------------------------------------- model-level events
# Emitted by ProviderAdapter.stream_response (spec §6.1) before they are lifted into
# NormalizedEvents by the API agent loop.


class ModelEventType(str, Enum):
    TEXT_DELTA = "text_delta"
    TOOL_CALL = "tool_call"
    USAGE = "usage"
    DONE = "done"
    ERROR = "error"


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    #: Tokens the model spent on reasoning (Codex ``usage.reasoning_output_tokens``, item 5). These
    #: are *counted*, never *contained* — no hidden reasoning text is stored anywhere.
    reasoning_tokens: int = 0
    provider_cost: float | None = None


class NormalizedModelEvent(BaseModel):
    """One event from a provider stream (spec §6.1)."""

    model_config = ConfigDict(extra="forbid")

    type: ModelEventType
    text: str | None = None
    tool_call: ToolCall | None = None
    usage: TokenUsage | None = None
    response_id: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    #: Provider reasoning is sensitive metadata — never rendered raw to the user (spec §6 note).
    reasoning: str | None = None
