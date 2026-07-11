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
    # run lifecycle
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    # sessions
    SESSION_CREATED = "session.created"
    SESSION_RESUMED = "session.resumed"
    # assistant messages
    MESSAGE_STARTED = "message.started"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"
    # tools
    TOOL_REQUESTED = "tool.requested"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    # shell commands
    COMMAND_STARTED = "command.started"
    COMMAND_OUTPUT = "command.output"
    COMMAND_COMPLETED = "command.completed"
    # file changes
    FILE_READ = "file.read"
    FILE_CREATED = "file.created"
    FILE_MODIFIED = "file.modified"
    FILE_DELETED = "file.deleted"
    # approvals
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_ACCEPTED = "approval.accepted"
    APPROVAL_DENIED = "approval.denied"
    # accounting / reliability
    USAGE_UPDATED = "usage.updated"
    RATE_LIMIT_DETECTED = "rate_limit.detected"
    RETRY_SCHEDULED = "retry.scheduled"
    # artifacts
    ARTIFACT_CREATED = "artifact.created"
    TEST_COMPLETED = "test.completed"
    # anything an adapter can't map cleanly
    LOG = "log"


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
