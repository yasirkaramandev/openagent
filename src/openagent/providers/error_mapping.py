"""Per-provider error refinement (spec §8.4).

:func:`~openagent.core.errors.classify_http_status` turns a status code into an error type, which is
as far as a status code can take you. It cannot tell an expired server-side session from a malformed
request (both 400), a spent balance from a forbidden key (402 at DeepSeek, 403 elsewhere), or a key
pointed at the wrong region from a key that is simply wrong (both 401). Those distinctions decide
what the user is told to *do*, and getting them wrong sends people to rotate credentials that were
never the problem.

So each provider names a refiner in its :class:`~.compat.profiles_v2.CompatibilityProfile`, and a
refiner obeys two rules:

* it may only **narrow** — return a more specific type, or ``None`` to keep the base;
* it never narrows on absence of evidence. An unrecognised payload keeps the status classification,
  because a refiner that guesses is worse than one that declines: the guess looks like a finding.

Refiners read only the status, the (already redacted) message text, and, where a provider signals in
its body rather than its status, a parsed body. They never see the credential.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..core.errors import ErrorType

#: Substrings that mean "this key is for the other endpoint" rather than "this key is invalid".
#: Kimi and Qwen both run separate international/China deployments where a key is valid for exactly
#: one, and both report the mismatch as a 401/403 (spec §16.2, §18.1).
_REGION_HINTS = (
    "different region",
    "wrong region",
    "region mismatch",
    "not available in this region",
    "not permitted in this region",
    "use the china endpoint",
    "use the international endpoint",
    "invalid region",
    "region is not",
)

_WORKSPACE_HINTS = ("workspace is not", "workspace not", "invalid workspace", "workspace mismatch")


@dataclass(frozen=True)
class ProviderErrorSignal:
    """Everything a refiner may look at.

    ``message`` has already been through :func:`~openagent.core.errors.redact_secrets` by the time a
    :class:`~.transport.TransportError` exists, so a refiner cannot be the place a key leaks.
    """

    status: int | None
    message: str
    base: ErrorType
    #: Parsed response body, for providers that report failures with HTTP 200 and a status code in
    #: the payload (MiniMax's ``base_resp``). ``None`` when the body was not JSON or not read.
    body: dict[str, Any] | None = None

    @property
    def text(self) -> str:
        return (self.message or "").lower()


Refiner = Callable[[ProviderErrorSignal], ErrorType | None]


# --------------------------------------------------------------------------- shared refiners


def _region_or_workspace(signal: ProviderErrorSignal) -> ErrorType | None:
    """A credential rejected because of *where* it points, not *what* it is."""

    if signal.status not in {401, 403}:
        return None
    text = signal.text
    if any(hint in text for hint in _REGION_HINTS):
        return ErrorType.PROVIDER_REGION_MISMATCH
    if any(hint in text for hint in _WORKSPACE_HINTS):
        return ErrorType.PROVIDER_REGION_MISMATCH
    return None


def _deprecated_model(signal: ProviderErrorSignal) -> ErrorType | None:
    """A model that still routes today but has been announced for removal.

    Reported separately from MODEL_NOT_FOUND because the request may currently succeed: the user
    needs to migrate before a date, not to fix a typo now (spec §15.1).
    """

    text = signal.text
    if "deprecat" in text or "retired" in text or "sunset" in text or "no longer supported" in text:
        return ErrorType.MODEL_DEPRECATED
    return None


def _balance(signal: ProviderErrorSignal) -> ErrorType | None:
    text = signal.text
    if signal.status == 402 or "insufficient balance" in text or "insufficient credit" in text:
        return ErrorType.INSUFFICIENT_BALANCE
    return None


def _local_server(signal: ProviderErrorSignal) -> ErrorType | None:
    """Shared Ollama/LM Studio conditions.

    Only a connection that failed to open becomes ``LOCAL_SERVER_UNAVAILABLE``: a *timeout* against
    a local server is a model that is slow or thrashing, not a daemon that is down, and "start the
    server" is unhelpful advice for a server that is already running (spec §12.4, §13.4).
    """

    text = signal.text
    if signal.base in {ErrorType.NETWORK_UNAVAILABLE, ErrorType.CONNECTION_LOST}:
        return ErrorType.LOCAL_SERVER_UNAVAILABLE
    if "out of memory" in text or "more system memory" in text or "not enough memory" in text:
        return ErrorType.LOCAL_MODEL_OUT_OF_MEMORY
    return None


# --------------------------------------------------------------------------- per-provider


def _openai(signal: ProviderErrorSignal) -> ErrorType | None:
    """The baseline. Refines only the two things every OpenAI-compatible endpoint gets wrong."""

    if signal.status == 400 and (
        "unsupported parameter" in signal.text
        or "unrecognized request argument" in signal.text
        or "unknown parameter" in signal.text
    ):
        return ErrorType.UNSUPPORTED_PARAMETER
    if signal.status in {400, 404}:
        return _deprecated_model(signal)
    return None


def _deepseek(signal: ProviderErrorSignal) -> ErrorType | None:
    """DeepSeek's documented status table (spec §15.4).

    402 is the interesting one: nothing else in the taxonomy's default mapping produces
    ``INSUFFICIENT_BALANCE``, so without this a spent account reads as a permissions problem.
    """

    balance = _balance(signal)
    if balance is not None:
        return balance
    if signal.status == 422:
        return ErrorType.INVALID_REQUEST
    if signal.status in {400, 404}:
        deprecated = _deprecated_model(signal)
        if deprecated is not None:
            return deprecated
    return _openai(signal)


def _kimi(signal: ProviderErrorSignal) -> ErrorType | None:
    region = _region_or_workspace(signal)
    if region is not None:
        return region
    if signal.status == 400 and "tool_choice" in signal.text:
        # Kimi rejects tool_choice=required. The profile already avoids sending it; if one arrives
        # anyway the cause is the request shape, not the prompt.
        return ErrorType.UNSUPPORTED_PARAMETER
    return _openai(signal)


def _qwen(signal: ProviderErrorSignal) -> ErrorType | None:
    region = _region_or_workspace(signal)
    if region is not None:
        return region
    if signal.status == 400 and "invalidparameter" in signal.text.replace(" ", ""):
        return ErrorType.INVALID_REQUEST
    return _openai(signal)


def _glm(signal: ProviderErrorSignal) -> ErrorType | None:
    text = signal.text
    if signal.status == 400 and ("tool_choice" in text or "tool_stream" in text):
        return ErrorType.UNSUPPORTED_PARAMETER
    balance = _balance(signal)
    if balance is not None:
        return balance
    return _openai(signal)


#: MiniMax reports application-level failures in ``base_resp.status_code`` with HTTP 200 (spec
#: §19.4). A caller that only reads the HTTP status treats these as successful empty completions.
_MINIMAX_CODES = {
    1000: ErrorType.UNKNOWN,
    1001: ErrorType.TIMEOUT,
    1002: ErrorType.PROVIDER_RATE_LIMITED,
    1004: ErrorType.AUTHENTICATION_FAILED,
    1008: ErrorType.INSUFFICIENT_BALANCE,
    1013: ErrorType.INVALID_REQUEST,
    1027: ErrorType.CONTENT_FILTERED,
    1039: ErrorType.PROVIDER_RATE_LIMITED,
    2013: ErrorType.INVALID_REQUEST,
}


def _minimax(signal: ProviderErrorSignal) -> ErrorType | None:
    body = signal.body or {}
    base_resp = body.get("base_resp") if isinstance(body.get("base_resp"), dict) else {}
    code = base_resp.get("status_code")
    if isinstance(code, int) and code != 0:
        mapped = _MINIMAX_CODES.get(code)
        if mapped is not None:
            return mapped
        # An unlisted non-zero code is still a failure — reporting it as success would hand the
        # caller an empty completion and no reason.
        return ErrorType.INVALID_REQUEST
    balance = _balance(signal)
    if balance is not None:
        return balance
    return _openai(signal)


def _openrouter(signal: ProviderErrorSignal) -> ErrorType | None:
    """OpenRouter is a router, so its failures are often about *routing*, not the model.

    "No allowed providers are available" means every upstream matching the route policy is down or
    excluded. That is an availability problem the user may resolve by relaxing the policy — not a
    missing model, which is what a bare 502 classification suggests.
    """

    text = signal.text
    balance = _balance(signal)
    if balance is not None:
        return balance
    if "no allowed providers" in text or "no providers available" in text:
        return ErrorType.PROVIDER_OVERLOADED
    if "no endpoints found" in text:
        return ErrorType.MODEL_NOT_FOUND
    if signal.status == 403 and "data policy" in text:
        return ErrorType.PERMISSION_DENIED
    return _openai(signal)


def _anthropic(signal: ProviderErrorSignal) -> ErrorType | None:
    text = signal.text
    if signal.status == 400 and ("thinking" in text or "unexpected" in text and "field" in text):
        return ErrorType.UNSUPPORTED_PARAMETER
    if signal.status == 529:
        return ErrorType.PROVIDER_OVERLOADED
    return None


def _gemini(signal: ProviderErrorSignal) -> ErrorType | None:
    text = signal.text
    if signal.status == 400 and (
        "previous_interaction_id" in text or ("interaction" in text and "not found" in text)
    ):
        return ErrorType.REMOTE_SESSION_EXPIRED
    if signal.status == 400 and ("unknown field" in text or "unsupported" in text):
        return ErrorType.UNSUPPORTED_PARAMETER
    if signal.status == 404 and "model" in text:
        return ErrorType.MODEL_NOT_FOUND
    if signal.status == 429 and "quota" in text:
        return ErrorType.PROVIDER_RATE_LIMITED
    return None


def _ollama(signal: ProviderErrorSignal) -> ErrorType | None:
    local = _local_server(signal)
    if local is not None:
        return local
    text = signal.text
    if signal.status == 404 and ("not found" in text or "pull" in text):
        # Distinct from LOCAL_MODEL_NOT_LOADED: the model is not on the machine at all, so the
        # remedy is a pull, which OpenAgent will not perform unasked (spec §12.4).
        return ErrorType.MODEL_NOT_FOUND
    return None


def _lmstudio(signal: ProviderErrorSignal) -> ErrorType | None:
    local = _local_server(signal)
    if local is not None:
        return local
    text = signal.text
    if "no models loaded" in text or "model is not loaded" in text or "lms load" in text:
        return ErrorType.LOCAL_MODEL_NOT_LOADED
    if signal.status == 404 and "model" in text:
        return ErrorType.MODEL_NOT_FOUND
    return None


_REFINERS: dict[str, Refiner] = {
    "openai": _openai,
    "anthropic": _anthropic,
    "deepseek": _deepseek,
    "kimi": _kimi,
    "qwen": _qwen,
    "glm": _glm,
    "minimax": _minimax,
    "openrouter": _openrouter,
    "gemini": _gemini,
    "ollama": _ollama,
    "lmstudio": _lmstudio,
}


def known_mappers() -> tuple[str, ...]:
    return tuple(_REFINERS)


def map_provider_error(mapper: str, signal: ProviderErrorSignal) -> ErrorType:
    """Refine ``signal.base`` using ``mapper``'s knowledge, or return it unchanged.

    An unknown mapper name is inert rather than an error: a provider row may name a refiner a newer
    build added, and failing the request over a missing *diagnostic* would be a worse outcome than
    reporting the less specific error.
    """

    refiner = _REFINERS.get(mapper)
    if refiner is None:
        return signal.base
    try:
        refined = refiner(signal)
    except Exception:  # noqa: BLE001 - a refiner is diagnostics; it must not mask the real failure
        return signal.base
    return refined if isinstance(refined, ErrorType) else signal.base
