"""Provider adapter contract + normalized request/response types (spec §6.1).

Every API provider — first-party (OpenAI, Anthropic) or an OpenAI-/Anthropic-compatible one —
implements :class:`ProviderAdapter`. The agent loop speaks only in the normalized types here, so it
never learns which provider it is talking to.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..core.events import NormalizedModelEvent, TokenUsage, ToolCall
from ..core.models import ModelCapabilities, RemoteModel


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Message(BaseModel):
    """A normalized conversation message."""

    model_config = ConfigDict(use_enum_values=True)

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    #: Provider-native content blocks preserved verbatim for fidelity (e.g. MiniMax requires the
    #: full assistant block list be echoed back — spec §19). Opaque to the loop.
    raw_blocks: list[dict[str, Any]] | None = None


class NormalizedModelRequest(BaseModel):
    """A provider-neutral model request (spec §6.1)."""

    model: str
    messages: list[Message] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    system: str = ""
    max_tokens: int = 4096
    temperature: float | None = None
    stream: bool = True


@dataclass
class HealthResult:
    ok: bool
    detail: str = ""


@dataclass
class TokenEstimate:
    input_tokens: int


@dataclass
class CollectedResponse:
    """The fully-assembled result of one model turn (what the agent loop consumes)."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage | None = None
    response_id: str | None = None
    error_type: str | None = None
    error_message: str | None = None

    @property
    def is_error(self) -> bool:
        return self.error_type is not None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@runtime_checkable
class ProviderAdapter(Protocol):
    """The five-method contract every provider implements (spec §6.1)."""

    async def test_connection(self) -> HealthResult: ...

    async def list_models(self) -> list[RemoteModel]: ...

    async def probe_model(self, model_id: str) -> ModelCapabilities: ...

    def stream_response(
        self, request: NormalizedModelRequest
    ) -> AsyncIterator[NormalizedModelEvent]: ...

    async def count_tokens(self, request: NormalizedModelRequest) -> TokenEstimate: ...


async def collect(events: AsyncIterator[NormalizedModelEvent]) -> CollectedResponse:
    """Consume a provider event stream into a single :class:`CollectedResponse`."""

    result = CollectedResponse()
    text_parts: list[str] = []
    async for event in events:
        if event.type == "text_delta" and event.text:
            text_parts.append(event.text)
        elif event.type == "tool_call" and event.tool_call is not None:
            result.tool_calls.append(event.tool_call)
        elif event.type == "usage" and event.usage is not None:
            result.usage = event.usage
        elif event.type == "error":
            result.error_type = event.error_type
            result.error_message = event.error_message
        if event.response_id:
            result.response_id = event.response_id
    result.text = "".join(text_parts)
    return result


def rough_token_estimate(request: NormalizedModelRequest) -> TokenEstimate:
    """A cheap local heuristic (~4 chars/token) used when a provider has no token endpoint."""

    chars = len(request.system)
    for message in request.messages:
        chars += len(message.content)
        for call in message.tool_calls:
            chars += len(str(call.arguments)) + len(call.name)
    return TokenEstimate(input_tokens=chars // 4)
