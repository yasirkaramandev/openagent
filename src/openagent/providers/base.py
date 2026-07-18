"""Provider adapter contract + normalized request/response types (spec §6.1).

Every API provider — first-party (OpenAI, Anthropic) or an OpenAI-/Anthropic-compatible one —
implements :class:`ProviderAdapter`. The agent loop speaks only in the normalized types here, so it
never learns which provider it is talking to.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..core.errors import ErrorType
from ..core.events import ModelEventType, NormalizedModelEvent, TokenUsage, ToolCall
from ..core.limits import RUNTIME_LIMITS
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
    #: EXPERIMENTAL (item 11, v0.1): a hook for provider-native content blocks echoed back verbatim
    #: (e.g. MiniMax's full assistant block list, spec §19). The Anthropic adapter honors this when a
    #: caller sets it, but the API agent loop does NOT populate it yet — so native fidelity is
    #: unverified. Do not rely on it; it is either wired end-to-end or removed in a later milestone.
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


class ModelCatalogError(ValueError):
    """A model endpoint returned malformed entries, optionally alongside usable ones."""

    def __init__(self, message: str, *, models: list[RemoteModel] | None = None) -> None:
        super().__init__(message)
        self.models = list(models or [])


def parse_model_catalog(
    payload: dict[str, Any], *, display_name_key: str = "id"
) -> list[RemoteModel]:
    """Strictly parse a conventional ``{"data": [...]}`` model catalog.

    Invalid entries are never silently reinterpreted as an empty catalog. Valid siblings are kept
    on :class:`ModelCatalogError`, allowing callers to report an honest partial result.
    """

    items = payload.get("data")
    if not isinstance(items, list):
        raise ModelCatalogError("provider model catalog has no data array")
    models: list[RemoteModel] = []
    malformed = 0
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"].strip()
        ):
            malformed += 1
            continue
        try:
            models.append(
                RemoteModel(
                    id=item["id"],
                    display_name=item.get(display_name_key) or item["id"],
                    owned_by=item.get("owned_by"),
                    context_window=item.get("context_window") or item.get("context_length"),
                )
            )
        except (TypeError, ValueError):
            malformed += 1
    if malformed:
        raise ModelCatalogError(
            f"provider model catalog contained {malformed} malformed entr{'y' if malformed == 1 else 'ies'}",
            models=models,
        )
    return models


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


def normalized_tool_call(
    *,
    call_id: object,
    name: object,
    arguments: object,
    response_id: str | None = None,
) -> NormalizedModelEvent:
    """Validate provider tool-call structure without inventing missing or malformed fields."""

    if (
        not isinstance(call_id, str)
        or not call_id.strip()
        or not isinstance(name, str)
        or not name.strip()
    ):
        return NormalizedModelEvent(
            type=ModelEventType.ERROR,
            error_type=ErrorType.INVALID_TOOL_CALL.value,
            error_message="provider tool call is missing a non-empty id or name",
            response_id=response_id,
        )
    parsed: object = arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return NormalizedModelEvent(
                type=ModelEventType.ERROR,
                error_type=ErrorType.INVALID_TOOL_ARGUMENTS.value,
                error_message="provider tool arguments are not valid JSON",
                response_id=response_id,
            )
    if not isinstance(parsed, dict):
        return NormalizedModelEvent(
            type=ModelEventType.ERROR,
            error_type=ErrorType.INVALID_TOOL_ARGUMENTS.value,
            error_message="provider tool arguments must be a JSON object",
            response_id=response_id,
        )
    try:
        size = len(json.dumps(parsed, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        size = RUNTIME_LIMITS.tool_arguments_bytes + 1
    if size > RUNTIME_LIMITS.tool_arguments_bytes:
        return NormalizedModelEvent(
            type=ModelEventType.ERROR,
            error_type=ErrorType.INVALID_TOOL_ARGUMENTS.value,
            error_message="provider tool arguments exceed 64 KiB",
            response_id=response_id,
        )
    return NormalizedModelEvent(
        type=ModelEventType.TOOL_CALL,
        tool_call=ToolCall(id=call_id, name=name, arguments=parsed),
        response_id=response_id,
    )


_PROBE_SENTINEL = "PROBE_OK_7F"


async def default_probe(
    adapter: Any, model_id: str, *, tool_probe: bool = True
) -> ModelCapabilities:
    """Honest capability probe shared by the model adapters (item 9).

    Each capability is claimed **only when actually observed** with the request shape that proves
    it — anything unproven stays ``None`` rather than defaulting to ``True``:

    * ``text`` + ``system_prompt`` — a non-stream request whose system prompt asks for a sentinel
      token; ``system_prompt`` is set only when the reply echoes it (the model demonstrably obeyed
      the system prompt);
    * ``streaming`` — a *real* streaming request; set only when it streams text back;
    * ``tool_calling`` — a tool-enabled request; set only when a tool call comes back.

    A transport/probe failure marks ``text=False`` and leaves the rest ``None``; a probe error never
    flips an unverified capability to ``True``.
    """

    # Starts wholly unknown: every field below is set only from something the probe actually
    # observed, so an early return cannot leave an optimistic default behind (spec §20).
    caps = ModelCapabilities(text=None, streaming=None, tool_calling=None, system_prompt=None)

    text_req = NormalizedModelRequest(
        model=model_id,
        system=f"You are a probe. Reply with exactly this token and nothing else: {_PROBE_SENTINEL}",
        messages=[Message(role=Role.USER, content="Follow your instructions.")],
        max_tokens=16,
        stream=False,
    )
    try:
        result = await collect(adapter.stream_response(text_req))
    except Exception:  # noqa: BLE001 - any failure -> capabilities unknown, assert nothing
        return ModelCapabilities(text=False)
    if result.is_error:
        return ModelCapabilities(text=False)
    caps.text = bool(result.text)
    if _PROBE_SENTINEL in (result.text or ""):
        caps.system_prompt = True

    stream_req = text_req.model_copy(update={"stream": True})
    try:
        sres = await collect(adapter.stream_response(stream_req))
        if not sres.is_error and sres.text:
            caps.streaming = True
    except Exception:  # noqa: BLE001 - leave streaming unknown, never True
        pass

    if tool_probe:
        tool_req = NormalizedModelRequest(
            model=model_id,
            messages=[Message(role=Role.USER, content="Call the ping tool with value 1.")],
            tools=[
                {
                    "name": "ping",
                    "description": "health probe",
                    "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}},
                }
            ],
            max_tokens=64,
            stream=False,
        )
        try:
            tres = await collect(adapter.stream_response(tool_req))
            if tres.tool_calls:
                caps.tool_calling = True
        except Exception:  # noqa: BLE001 - absence/err doesn't disprove support -> leave None
            pass

    return caps


def rough_token_estimate(request: NormalizedModelRequest) -> TokenEstimate:
    """A cheap local heuristic (~4 chars/token) used when a provider has no token endpoint."""

    chars = len(request.system)
    for message in request.messages:
        chars += len(message.content)
        for call in message.tool_calls:
            chars += len(str(call.arguments)) + len(call.name)
    return TokenEstimate(input_tokens=chars // 4)
