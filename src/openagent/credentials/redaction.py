"""Secret redaction for logs and events (spec §30).

Every string written to ``logs.txt`` / ``events.jsonl`` passes through :func:`redact` first so API
keys never land on disk. Patterns cover the common shapes: ``sk-...``, ``Bearer ...``,
``api_key=...``, ``Authorization: ...`` and ``*_API_KEY=...``.
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


def redact(text: str) -> str:
    """Return ``text`` with secret-looking substrings replaced by ``[REDACTED]``."""

    if not text:
        return text
    result = text
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

    return _walk(data)  # type: ignore[return-value]


def _walk(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _walk(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v) for v in value]
    return value
