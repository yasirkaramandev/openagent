"""Gemini Interactions API adapter (spec §10).

The Interactions API is Google's unified interface for Gemini models and agents. It differs from
every other adapter here in two ways that matter to OpenAgent.

**A turn is a sequence of steps, not a message.** Thoughts, function calls, function results and the
final model output are all steps, each announced by ``step.start``, filled in by ``step.delta`` and
closed by ``step.stop``. Flattening that into "text plus tool calls" is lossy in a specific way:
the thought steps carry ``thought_signature`` values that a *stateless* continuation has to replay
verbatim, and a normalized transcript does not have them. So the native steps are captured
alongside the normalized events rather than instead of them.

**The provider will hold the conversation for you, and that is a privacy decision.** ``store``
governs whether Google retains the interaction, and ``previous_interaction_id`` is only usable if it
did. OpenAgent therefore defaults to ``store=false`` and treats server-side state as something the
user turns on knowingly (spec §10.4) — the alternative is that resuming a conversation quietly
starts depending on the provider having kept it.

What is verified and what is not
--------------------------------
Endpoint, auth header, request fields, tool/function-result shapes, streaming event names, delta
shapes and usage field names are taken from Google's published documentation (``POST
/v1beta/interactions``, ``x-goog-api-key``). Two things are *not* settled by the docs this was
written against and are therefore marked for live verification rather than assumed correct:

* the SSE framing of the streamed response (``data:`` lines), inferred from the ``done`` sentinel
  and the endpoint family's convention;
* whether ``/v1beta/models`` lists Interactions-capable models with the same metadata it reports
  for ``generateContent``.

Both are exercised by the live smoke; neither is claimed as supported until it runs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..core.errors import ErrorType
from ..core.events import ModelEventType, NormalizedModelEvent, TokenUsage
from ..core.models import ModelCapabilities, Protocol, RemoteModel
from .base import (
    HealthResult,
    Message,
    ModelCatalogError,
    NormalizedModelRequest,
    Role,
    TokenEstimate,
    default_probe,
    normalized_tool_call,
    rough_token_estimate,
)
from .continuation import ContinuationEnvelope, ContinuationStrategy
from .transport import Transport, TransportError

#: Google's public endpoint for the Interactions API.
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

#: The catalog page size. The documented maximum is 1000; asking for it keeps the common case to a
#: single request while the pagination loop below still handles a provider that returns fewer.
_MODEL_PAGE_SIZE = 1000

#: Refuse to page forever if a provider keeps handing back a next-page token.
_MAX_MODEL_PAGES = 20

_STEP_MODEL_OUTPUT = "model_output"
_STEP_FUNCTION_CALL = "function_call"
_STEP_THOUGHT = "thought"


class GeminiInteractionsAdapter:
    """Speaks the Interactions API and emits OpenAgent's normalized events."""

    provider_type = "gemini"
    protocol = Protocol.GEMINI_INTERACTIONS

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str = DEFAULT_BASE_URL,
        store: bool = False,
        previous_interaction_id: str | None = None,
        transport: Transport | None = None,
    ) -> None:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            # Header rather than the ``?key=`` query parameter the quickstarts use. A key in a URL
            # lands in proxy logs, crash reports and anything that records a request line; a header
            # is the same auth with none of that reach.
            headers["x-goog-api-key"] = api_key
        self.transport = transport or Transport(base_url=base_url.rstrip("/"), headers=headers)
        #: Whether Google retains this conversation. False keeps state local (spec §10.4).
        self.store = store
        self.previous_interaction_id = previous_interaction_id
        #: Native steps from the most recent turn, kept for stateless continuation replay.
        self._native_steps: list[dict[str, Any]] = []
        self._last_interaction_id: str | None = None

    # ------------------------------------------------------------------ health / discovery

    async def test_connection(self) -> HealthResult:
        try:
            await self.transport.get_json(f"/models?pageSize={_MODEL_PAGE_SIZE}")
            return HealthResult(ok=True, detail="reachable")
        except TransportError as exc:
            return HealthResult(ok=False, detail=exc.message)

    async def list_models(self) -> list[RemoteModel]:
        """Page through ``/models``.

        Raises :class:`ModelCatalogError` carrying whatever parsed when some entries did not, so the
        caller can report an honest partial catalog. An unreadable catalog is never flattened into
        an empty one — "this provider has no models" and "we could not read the list" look identical
        to a user and mean completely different things (spec §25.3).
        """

        models: list[RemoteModel] = []
        malformed = 0
        page_token: str | None = None

        for _ in range(_MAX_MODEL_PAGES):
            path = f"/models?pageSize={_MODEL_PAGE_SIZE}"
            if page_token:
                path = f"{path}&pageToken={page_token}"
            payload = await self.transport.get_json(path)

            entries = payload.get("models")
            if not isinstance(entries, list):
                raise ModelCatalogError("Gemini model catalog has no models array", models=models)
            for entry in entries:
                parsed = _parse_model(entry)
                if parsed is None:
                    malformed += 1
                else:
                    models.append(parsed)

            next_token = payload.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token.strip():
                break
            page_token = next_token
        else:
            raise ModelCatalogError(
                f"Gemini model catalog did not terminate within {_MAX_MODEL_PAGES} pages",
                models=models,
            )

        if malformed:
            raise ModelCatalogError(
                f"Gemini model catalog contained {malformed} malformed "
                f"entr{'y' if malformed == 1 else 'ies'}",
                models=models,
            )
        return models

    async def probe_model(self, model_id: str) -> ModelCapabilities:
        return await default_probe(self, model_id)

    async def count_tokens(self, request: NormalizedModelRequest) -> TokenEstimate:
        """A local heuristic.

        The Interactions API surface this adapter targets does not document a token-counting
        endpoint, so this is an estimate and is reported as one rather than dressed up as a
        provider-authoritative count.
        """

        return rough_token_estimate(request)

    # ------------------------------------------------------------------ streaming

    async def stream_response(
        self, request: NormalizedModelRequest
    ) -> AsyncIterator[NormalizedModelEvent]:
        self._native_steps = []
        self._last_interaction_id = None
        payload = self.build_payload(request)
        try:
            if request.stream:
                async for event in self._stream(payload):
                    yield event
            else:
                async for event in self._complete(payload):
                    yield event
        except TransportError as exc:
            yield NormalizedModelEvent(
                type=ModelEventType.ERROR,
                error_type=_map_error(exc).value,
                error_message=exc.message,
                response_id=self._last_interaction_id,
            )

    async def _complete(self, payload: dict[str, Any]) -> AsyncIterator[NormalizedModelEvent]:
        payload = {**payload, "stream": False}
        data = await self.transport.post_json("/interactions", payload)
        self._last_interaction_id = _string_or_none(data.get("id"))

        steps = data.get("steps")
        if isinstance(steps, list):
            for step in steps:
                if not isinstance(step, dict):
                    continue
                self._native_steps.append(step)
                async for event in self._events_for_completed_step(step):
                    yield event

        usage = _parse_usage(data.get("usage"))
        if usage is not None:
            yield NormalizedModelEvent(
                type=ModelEventType.USAGE, usage=usage, response_id=self._last_interaction_id
            )
        yield NormalizedModelEvent(type=ModelEventType.DONE, response_id=self._last_interaction_id)

    async def _events_for_completed_step(
        self, step: dict[str, Any]
    ) -> AsyncIterator[NormalizedModelEvent]:
        step_type = step.get("type")
        if step_type == _STEP_MODEL_OUTPUT:
            text = _text_from_content(step)
            if text:
                yield NormalizedModelEvent(
                    type=ModelEventType.TEXT_DELTA,
                    text=text,
                    response_id=self._last_interaction_id,
                )
        elif step_type == _STEP_FUNCTION_CALL:
            yield normalized_tool_call(
                call_id=step.get("id"),
                name=step.get("name"),
                arguments=step.get("arguments", {}),
                response_id=self._last_interaction_id,
            )
        # A thought step emits nothing. See _stream() for why.

    async def _stream(self, payload: dict[str, Any]) -> AsyncIterator[NormalizedModelEvent]:
        payload = {**payload, "stream": True}
        #: Function-call steps arrive as a start event naming the call, then argument fragments, then
        #: a stop. Fragments are accumulated per step index and parsed once at the stop — a fragment
        #: is not JSON, and asking whether it is eventually says yes to something malformed.
        pending: dict[int, dict[str, Any]] = {}
        usage: TokenUsage | None = None

        async for event in self.transport.stream_sse("/interactions", payload):
            kind = event.get("type")

            if kind == "interaction.created":
                self._last_interaction_id = _string_or_none(
                    event.get("id") or _dict(event.get("interaction")).get("id")
                )
                continue

            if kind == "step.start":
                index = _index_of(event)
                step = _dict(event.get("step"))
                pending[index] = {
                    "type": step.get("type"),
                    "id": step.get("id"),
                    "name": step.get("name"),
                    "arguments_text": "",
                    "text": "",
                    "thought_summary": "",
                    "thought_signature": None,
                }
                continue

            if kind == "step.delta":
                index = _index_of(event)
                draft = pending.setdefault(
                    index,
                    {
                        "type": None,
                        "id": None,
                        "name": None,
                        "arguments_text": "",
                        "text": "",
                        "thought_summary": "",
                        "thought_signature": None,
                    },
                )
                delta = _dict(event.get("delta"))
                delta_type = delta.get("type")

                if delta_type == "text":
                    text = delta.get("text")
                    if isinstance(text, str) and text:
                        draft["text"] += text
                        yield NormalizedModelEvent(
                            type=ModelEventType.TEXT_DELTA,
                            text=text,
                            response_id=self._last_interaction_id,
                        )
                elif delta_type == "arguments_delta":
                    fragment = delta.get("arguments")
                    if isinstance(fragment, str):
                        draft["arguments_text"] += fragment
                elif delta_type == "thought_summary":
                    # Accumulated for replay, never emitted. `openai_chat` already drops reasoning
                    # so it "never reaches an event, artifact, or the UI" (spec §12), and no
                    # consumer reads NormalizedModelEvent.reasoning today. Emitting it here would
                    # make Gemini the one provider whose thoughts surface the moment some future
                    # renderer starts reading that field — an inconsistency nobody chose. Showing
                    # thought summaries is a decision to make deliberately, with a real event type
                    # and every adapter changed together.
                    summary = _content_text(delta.get("content"))
                    if summary:
                        draft["thought_summary"] += summary
                elif delta_type == "thought_signature":
                    # Opaque and required verbatim for stateless replay. Never shown, never parsed.
                    signature = delta.get("signature")
                    if isinstance(signature, str):
                        draft["thought_signature"] = signature
                continue

            if kind == "step.stop":
                index = _index_of(event)
                stopped = pending.pop(index, None)
                if stopped is not None:
                    self._native_steps.append(_native_step(stopped))
                    if stopped["type"] == _STEP_FUNCTION_CALL:
                        yield normalized_tool_call(
                            call_id=stopped.get("id"),
                            name=stopped.get("name"),
                            arguments=stopped.get("arguments_text") or {},
                            response_id=self._last_interaction_id,
                        )
                step_usage = _parse_usage(event.get("usage"))
                if step_usage is not None:
                    usage = step_usage
                continue

            if kind == "interaction.completed":
                # The completed interaction may be nested or inlined; both shapes appear in the
                # documented examples, so neither is assumed.
                interaction = _dict(event.get("interaction")) or event
                self._last_interaction_id = (
                    _string_or_none(interaction.get("id")) or self._last_interaction_id
                )
                completed_usage = _parse_usage(interaction.get("usage"))
                if completed_usage is not None:
                    usage = completed_usage
                continue

            if kind == "error":
                error = _dict(event.get("error"))
                yield NormalizedModelEvent(
                    type=ModelEventType.ERROR,
                    error_type=_map_stream_error(error).value,
                    error_message=str(error.get("message") or "Gemini reported an error"),
                    response_id=self._last_interaction_id,
                )
                return

            if kind == "done":
                break

        # A step that never received its stop is an unfinished step, not a discarded one: its
        # partial native material is still what a stateless replay would need to describe the turn.
        for leftover in pending.values():
            self._native_steps.append(_native_step(leftover))

        if usage is not None:
            yield NormalizedModelEvent(
                type=ModelEventType.USAGE, usage=usage, response_id=self._last_interaction_id
            )
        yield NormalizedModelEvent(type=ModelEventType.DONE, response_id=self._last_interaction_id)

    # ------------------------------------------------------------------ payload

    def build_payload(self, request: NormalizedModelRequest) -> dict[str, Any]:
        """Serialize a normalized request into an Interactions request body."""

        payload: dict[str, Any] = {
            "model": request.model,
            "input": _build_input(request.messages),
            "store": self.store,
        }
        if request.system:
            payload["system_instruction"] = request.system
        if request.tools:
            payload["tools"] = [_function_tool(tool) for tool in request.tools]

        generation_config: dict[str, Any] = {}
        if request.max_tokens:
            generation_config["max_output_tokens"] = request.max_tokens
        if request.temperature is not None:
            generation_config["temperature"] = request.temperature
        if generation_config:
            payload["generation_config"] = generation_config

        if self.previous_interaction_id:
            if not self.store:
                # The provider can only look up an interaction it was asked to keep. Sending the id
                # anyway produces a confusing provider-side error about an unknown interaction, when
                # the real cause is a local setting.
                raise ValueError(
                    "previous_interaction_id requires store=True; with local-only state, resume by "
                    "replaying the recorded native steps instead"
                )
            payload["previous_interaction_id"] = self.previous_interaction_id
        return payload

    # ------------------------------------------------------------------ continuation

    def build_continuation(self, *, model_id: str | None = None) -> ContinuationEnvelope:
        """Describe how the turn just streamed can be continued (spec §10, §26).

        Two honest outcomes, chosen by what actually happened rather than by what the provider is
        capable of. With ``store=True`` and an interaction id, the provider holds the conversation
        and the id is enough. Otherwise the native steps — including the thought signatures a
        normalized transcript cannot represent — are replayed, which is why they were captured.
        """

        if self.store and self._last_interaction_id:
            return ContinuationEnvelope.build(
                provider_type=self.provider_type,
                protocol=self.protocol,
                strategy=ContinuationStrategy.REMOTE_ID,
                remote_interaction_id=self._last_interaction_id,
                model_id=model_id,
            )
        if self._native_steps:
            return ContinuationEnvelope.build(
                provider_type=self.provider_type,
                protocol=self.protocol,
                strategy=ContinuationStrategy.NATIVE_STEPS_REPLAY,
                native_steps=self._native_steps,
                model_id=model_id,
            )
        return ContinuationEnvelope.build(
            provider_type=self.provider_type,
            protocol=self.protocol,
            strategy=ContinuationStrategy.NORMALIZED_HISTORY,
            model_id=model_id,
        )


# ------------------------------------------------------------------------------- helpers


def _parse_model(entry: object) -> RemoteModel | None:
    """One catalog entry, or ``None`` if it is not usable.

    Gemini reports ``models/<id>``; the bare id is what a user types and what a request carries, so
    the prefix is stripped here rather than in four call sites.
    """

    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    model_id = name.split("/", 1)[1] if name.startswith("models/") else name
    if not model_id:
        return None
    # RemoteModel permits extra fields, so Gemini's own metadata rides along for the wizard to
    # filter on. Built as a mapping because the extras are provider-specific by definition and
    # naming them as keyword arguments would claim they are part of the shared model.
    fields: dict[str, Any] = {
        "id": model_id,
        "display_name": entry.get("displayName") or model_id,
        "context_window": _positive_int(entry.get("inputTokenLimit")),
        "output_token_limit": _positive_int(entry.get("outputTokenLimit")),
        "supported_generation_methods": entry.get("supportedGenerationMethods") or [],
        "version": entry.get("version"),
        "description": entry.get("description"),
    }
    try:
        return RemoteModel(**fields)
    except (TypeError, ValueError):
        return None


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _build_input(messages: list[Message]) -> list[dict[str, Any]]:
    """Turn normalized messages into Interactions input items.

    Tool results become ``function_result`` items keyed by ``call_id``; a result whose call id was
    lost cannot be matched to its call, and sending it unkeyed would attach it to the wrong one.
    """

    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.role.value if hasattr(message.role, "value") else str(message.role)

        if role == Role.TOOL.value:
            if not message.tool_call_id:
                raise ValueError("a tool result needs the call_id of the call it answers")
            items.append(
                {
                    "type": "function_result",
                    "name": message.name or "",
                    "call_id": message.tool_call_id,
                    "result": [{"type": "text", "text": message.content or ""}],
                }
            )
            continue

        if role == Role.ASSISTANT.value:
            if message.raw_blocks:
                # Native steps replayed verbatim — the whole point of capturing them.
                items.extend(message.raw_blocks)
                continue
            for call in message.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                )
            if message.content:
                items.append({"role": "model", "parts": [{"text": message.content}]})
            continue

        if message.content:
            items.append({"role": "user", "parts": [{"text": message.content}]})
    return items


def _function_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Wrap a normalized tool schema in the Interactions ``type: function`` discriminator."""

    return {
        "type": "function",
        "name": tool.get("name", ""),
        "description": tool.get("description", ""),
        "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
    }


def _dict(value: object) -> dict[str, Any]:
    """``value`` if it is a mapping, else an empty one.

    Providers omit optional objects rather than sending them empty, so every access to a nested
    object needs this guard; funnelling it through one helper keeps the stream loop readable and
    gives the type checker something it can actually narrow.
    """

    return value if isinstance(value, dict) else {}


def _index_of(event: dict[str, Any]) -> int:
    index = event.get("index")
    return index if isinstance(index, int) and not isinstance(index, bool) else 0


def _native_step(draft: dict[str, Any]) -> dict[str, Any]:
    """The native form of an assembled step, for replay. Empty fields are omitted, not nulled."""

    step: dict[str, Any] = {"type": draft.get("type")}
    for key in ("id", "name", "thought_signature"):
        if draft.get(key):
            step[key] = draft[key]
    if draft.get("arguments_text"):
        step["arguments"] = draft["arguments_text"]
    if draft.get("text"):
        step["text"] = draft["text"]
    if draft.get("thought_summary"):
        step["thought_summary"] = draft["thought_summary"]
    return step


def _text_from_content(step: dict[str, Any]) -> str:
    text = step.get("text")
    if isinstance(text, str):
        return text
    return _content_text(step.get("content"))


def _content_text(content: object) -> str:
    """Pull display text out of a content block, a list of them, or a bare string."""

    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(content, list):
        return "".join(_content_text(item) for item in content)
    return ""


def _thought_summary_text(step: dict[str, Any]) -> str:
    summary = step.get("thought_summary")
    if summary is not None:
        return _content_text(summary)
    return _content_text(step.get("content"))


def _parse_usage(raw: object) -> TokenUsage | None:
    """Map Interactions usage onto OpenAgent's counters.

    ``total_thought_tokens`` is billed output the user never sees, so it is recorded separately
    rather than folded into output tokens — otherwise a reasoning-heavy turn looks like it produced
    far more text than it did.
    """

    if not isinstance(raw, dict):
        return None
    fields = {
        "input_tokens": _non_negative(raw.get("total_input_tokens")),
        "cached_input_tokens": _non_negative(raw.get("total_cached_tokens")),
        "output_tokens": _non_negative(raw.get("total_output_tokens")),
        "reasoning_tokens": _non_negative(raw.get("total_thought_tokens")),
    }
    if all(value == 0 for value in fields.values()) and not _non_negative(raw.get("total_tokens")):
        return None
    return TokenUsage(**fields)


def _non_negative(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _string_or_none(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _map_error(exc: TransportError) -> ErrorType:
    """Refine a transport failure using what Gemini puts in the message.

    A 400 mentioning an unknown interaction is a dead server-side session, not a malformed request,
    and telling the user to fix their prompt would send them after the wrong thing.
    """

    text = (exc.message or "").lower()
    if exc.status == 400 and (
        "previous_interaction_id" in text or ("interaction" in text and "not found" in text)
    ):
        return ErrorType.REMOTE_SESSION_EXPIRED
    if exc.status == 404 and "model" in text:
        return ErrorType.MODEL_NOT_FOUND
    if exc.status == 400 and ("unsupported" in text or "unknown field" in text):
        return ErrorType.UNSUPPORTED_PARAMETER
    return exc.error_type


def _map_stream_error(error: dict[str, Any]) -> ErrorType:
    code = error.get("code")
    if isinstance(code, int):
        from ..core.errors import classify_http_status

        return classify_http_status(code)
    return ErrorType.UNKNOWN
