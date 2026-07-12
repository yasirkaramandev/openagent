"""Secret redaction for artifacts, logs, events, and UI output (spec §30).

Every string written to a run artifact (``request.json`` — including the user prompt — ``result.json``,
``events.jsonl``, ``logs.txt``, ``changes.diff``, ``output.md``, ``handoff.md``…) and every line the
TUI/CLI print passes through :func:`redact` first so secrets never land on disk or screen.

Two layers:

* **Pattern-based** — common key shapes (``sk-...``, ``Bearer ...``, ``api_key=...``,
  ``Authorization: ...``, GitHub tokens…).
* **Exact registered values** — because some providers (e.g. several Chinese providers) issue keys
  with no recognizable prefix, callers register the concrete key value at runtime with
  :func:`register_secret`; the exact string is then scrubbed anywhere it appears.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_PATTERNS: list[re.Pattern[str]] = [
    # Provider key prefixes: sk-..., sk-ant-..., gsk_..., etc. (>=16 trailing chars)
    re.compile(r"\b(sk|rk|gsk|xai|glm|ya29)[-_][A-Za-z0-9_\-]{16,}"),
    # Bearer tokens
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
    # Authorization headers
    re.compile(r"(?i)\bAuthorization\s*:\s*\S+"),
    # key=value forms for anything ending in _api_key / api_key / token / secret
    re.compile(r"(?i)\b([A-Z0-9_]*API[_-]?KEY|token|secret|password)\s*[=:]\s*[^\s\"']+"),
    # GitHub-style tokens
    re.compile(r"\bgh[posru]_[A-Za-z0-9]{16,}"),
]

#: Exact secret values registered at runtime (values, not references). Long strings only — short
#: values would over-match and corrupt output.
_REGISTERED: set[str] = set()
_MIN_REGISTER_LEN = 8


def register_secret(secret: str | None) -> None:
    """Register an exact secret value to scrub from all output (spec §30).

    Required for provider keys whose format has no recognizable prefix. Safe to call repeatedly.
    """

    if secret and len(secret) >= _MIN_REGISTER_LEN:
        _REGISTERED.add(secret)


def clear_registered_secrets() -> None:
    _REGISTERED.clear()


def redact(text: str) -> str:
    """Return ``text`` with secret-looking substrings replaced by ``[REDACTED]``."""

    if not text:
        return text
    result = text
    # Exact registered values first (covers prefixless keys the patterns can't match).
    for secret in sorted(_REGISTERED, key=len, reverse=True):
        if secret in result:
            result = result.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        result = pattern.sub(_replace, result)
    return result


def _replace(match: re.Match[str]) -> str:
    whole = match.group(0)
    # Preserve a leading label (e.g. "Authorization:", "OPENAI_API_KEY=") for readability.
    for sep in (":", "="):
        if sep in whole:
            label, _, _rest = whole.partition(sep)
            if label and not label.strip().lower().startswith(("bearer",)):
                return f"{label}{sep} {REDACTED}"
    return REDACTED


def redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact string values inside a JSON-serializable mapping."""

    return _walk(data)


def _walk(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _walk(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v) for v in value]
    return value
