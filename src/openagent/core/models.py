"""Core domain models (spec §3).

The important separation the whole system rests on:

* :class:`ProviderConnection` — an API account or local service. **No role, no prompt.**
* :class:`ModelProfile` — a concrete model on a provider, plus probed capabilities.
* :class:`AgentProfile` — what the user actually runs; binds a runtime (API or CLI) + prompt + tags
  + permission profile. Many agents can share one provider/model; the API key is stored once.
* :class:`CliInstallation` — a coding CLI detected on this machine.
* :class:`Run` / :class:`Session` — an execution and its resumable conversation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- enums


class RuntimeType(str, Enum):
    API_AGENT = "api-agent"
    CLI = "cli"


class Protocol(str, Enum):
    OPENAI_CHAT = "openai-chat"
    OPENAI_RESPONSES = "openai-responses"
    ANTHROPIC_MESSAGES = "anthropic-messages"


class RunStatus(str, Enum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"


TERMINAL_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}


class CredentialType(str, Enum):
    KEYCHAIN = "keychain"
    ENV = "env"
    SESSION = "session"
    EXTERNAL_COMMAND = "external-command"
    NONE = "none"


# --------------------------------------------------------------------------- capabilities


class ModelCapabilities(BaseModel):
    """Probed capabilities for a model (spec §25). ``None`` means "not yet determined"."""

    model_config = ConfigDict(extra="forbid")

    text: bool = True
    streaming: bool | None = None
    tool_calling: bool | None = None
    parallel_tool_calling: bool | None = None
    structured_output: bool | None = None
    vision: bool | None = None
    system_prompt: bool | None = None

    def merge(self, other: ModelCapabilities) -> ModelCapabilities:
        """Overlay non-null values from ``other`` (probe results win over presets)."""
        data = self.model_dump()
        for key, value in other.model_dump().items():
            if value is not None:
                data[key] = value
        return ModelCapabilities(**data)


class RemoteModel(BaseModel):
    """A model as reported by a provider's ``/models`` endpoint (spec §25.1)."""

    model_config = ConfigDict(extra="allow")

    id: str
    display_name: str | None = None
    context_window: int | None = None


# --------------------------------------------------------------------------- credentials


class CredentialRef(BaseModel):
    """A pointer to a secret — never the secret itself (spec §30)."""

    model_config = ConfigDict(extra="forbid")

    type: CredentialType = CredentialType.KEYCHAIN
    service: str = "openagent"
    account: str | None = None
    env_var: str | None = None
    command: list[str] | None = None


# --------------------------------------------------------------------------- providers/models


class ProviderConnection(BaseModel):
    """An API account or local service. Contains no role or prompt (spec §3.1)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    provider_type: str
    protocol: Protocol = Protocol.OPENAI_CHAT
    base_url: str | None = None
    anthropic_base_url: str | None = None
    credential: CredentialRef = Field(default_factory=CredentialRef)
    region: str | None = None
    workspace_id: str | None = None
    enabled: bool = True
    extra_headers: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class ModelProfile(BaseModel):
    """A concrete model on a provider (spec §3.2)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    provider_connection: str
    remote_model_id: str
    deployment_id: str | None = None  # ByteDance Ark etc. (spec §21)
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    capabilities_tested_at: datetime | None = None
    context_window: int | None = None


# --------------------------------------------------------------------------- agents


class AgentRuntime(BaseModel):
    """How an agent executes: an API model, or an installed CLI."""

    model_config = ConfigDict(extra="forbid")

    type: RuntimeType
    # API-agent fields
    provider: str | None = None
    model: str | None = None
    # CLI-agent fields
    cli: str | None = None


class AgentProfile(BaseModel):
    """What the user runs (spec §3.3)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    title: str = ""
    description: str = ""
    runtime: AgentRuntime
    tags: list[str] = Field(default_factory=list)
    system_prompt: str = ""
    permission_profile: str = "safe-edit"
    max_steps: int = 40
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- CLI installs


class CliInstallation(BaseModel):
    """A coding CLI detected on this machine (spec §3.4)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str  # codex | claude | gemini | agy | ...
    executable: str
    version: str | None = None
    authenticated: bool | None = None
    adapter: str = ""
    experimental: bool = False


# --------------------------------------------------------------------------- runs/sessions


class Run(BaseModel):
    """A single execution (spec §3.5)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    agent: str
    status: RunStatus = RunStatus.QUEUED
    workspace: str = ""
    worktree: str | None = None
    branch: str | None = None
    base_commit: str | None = None
    permission_profile: str = "safe-edit"
    prompt: str = ""
    provider_session_id: str | None = None
    session_id: str | None = None
    pid: int | None = None
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    exit_code: int | None = None
    failure_type: str | None = None
    files_changed: list[str] = Field(default_factory=list)


class Session(BaseModel):
    """A resumable conversation for a CLI or API runtime (spec §3.6)."""

    model_config = ConfigDict(extra="forbid")

    openagent_session_id: str
    runtime: str
    provider_session_id: str | None = None
    workspace: str = ""
    created_at: datetime = Field(default_factory=utcnow)
