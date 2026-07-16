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
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

REDACTED = "[REDACTED]"

_PATTERNS: list[re.Pattern[str]] = [
    # Provider key prefixes: sk-..., sk-ant-..., gsk_..., etc. (>=16 trailing chars)
    re.compile(r"\b(sk|rk|gsk|xai|glm|ya29)[-_][A-Za-z0-9_\-]{16,}"),
    # NVIDIA Build keys (spec §19). Format may evolve; the prefix is a hint, so match it defensively
    # in addition to the exact registered value.
    re.compile(r"\bnvapi-[A-Za-z0-9_\-]{8,}"),
    # Bearer tokens
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
    # Authorization headers
    re.compile(r"(?i)\bAuthorization\s*:\s*\S+"),
    # key=value forms for anything ending in _api_key / api_key / token / secret
    re.compile(r"(?i)\b([A-Z0-9_]*API[_-]?KEY|token|secret|password)\s*[=:]\s*[^\s\"']+"),
    # GitHub-style tokens
    re.compile(r"\bgh[posru]_[A-Za-z0-9]{16,}"),
]

#: Short values would over-match and corrupt ordinary output ("abc" appears in prose).
_MIN_REGISTER_LEN = 8


class _SecretRegistry:
    """Exact secret values held for the lifetime of the runs that need them (spec §8).

    This replaces a module-level ``set`` that was never scoped, never emptied, and was iterated by
    ``redact()`` while other threads could add to it — ``sorted(_REGISTERED, ...)`` raises
    ``RuntimeError: Set changed size during iteration`` if it loses that race, in the one function
    whose job is to stop keys escaping.

    Two design points:

    * **Reference counted.** Two concurrent runs can hold the same provider key; the first to finish
      must not un-redact it for the second. A count, not a set, decides when a value is really gone.
    * **Copy-on-write snapshot.** ``redact()`` is on the hot path — every artifact write and every UI
      string — so it must not take a lock. Mutations rebuild an immutable, longest-first tuple under
      the lock; readers bind that tuple once and iterate it. Rebinding a name is atomic in CPython,
      so a reader either sees the old snapshot or the new one, never a half-mutated container.

    Longest-first matters: if one key is a prefix of another, replacing the short one first would
    leave the tail of the long one on screen.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counts: dict[str, int] = {}
        self._snapshot: tuple[str, ...] = ()

    def _rebuild(self) -> None:
        self._snapshot = tuple(sorted(self._counts, key=len, reverse=True))

    def acquire(self, secret: str | None) -> str | None:
        """Register ``secret`` (or bump its refcount). Returns it if it was actually taken."""

        if not secret or len(secret) < _MIN_REGISTER_LEN:
            return None
        with self._lock:
            self._counts[secret] = self._counts.get(secret, 0) + 1
            self._rebuild()
        return secret

    def release(self, secret: str | None) -> None:
        """Drop one reference; forget the value entirely when the last holder is gone."""

        if not secret:
            return
        with self._lock:
            count = self._counts.get(secret)
            if count is None:
                return
            if count <= 1:
                del self._counts[secret]
            else:
                self._counts[secret] = count - 1
            self._rebuild()

    def snapshot(self) -> tuple[str, ...]:
        return self._snapshot

    def count(self) -> int:
        with self._lock:
            return len(self._counts)

    def clear(self) -> None:
        with self._lock:
            self._counts.clear()
            self._rebuild()

    def __repr__(self) -> str:
        # Never render the values. A traceback that captures locals, or a debugger, would otherwise
        # print every live API key (spec §8).
        return f"<SecretRegistry: {len(self._counts)} active>"


_SECRETS = _SecretRegistry()


def register_secret(secret: str | None) -> None:
    """Register an exact secret value to scrub from all output (spec §8).

    Required for provider keys whose format has no recognizable prefix. Safe to call repeatedly.

    This takes a reference that is never released; prefer :func:`secret_scope`, which ties the value
    to the run that needs it. It remains for callers that genuinely cannot bound the lifetime.
    """

    _SECRETS.acquire(secret)


def release_secret(secret: str | None) -> None:
    """Drop one reference taken by :func:`register_secret`."""

    _SECRETS.release(secret)


@contextmanager
def secret_scope(*secrets: str | None) -> Iterator[None]:
    """Scrub ``secrets`` for the duration of the block, then forget them (spec §8).

    Reference counted, so overlapping runs sharing a key are safe, and released on the way out of an
    exception as well as a clean exit — a failed run is exactly when the key is most likely to appear
    in an error body.
    """

    taken = [s for s in (_SECRETS.acquire(s) for s in secrets) if s is not None]
    try:
        yield
    finally:
        for secret in taken:
            _SECRETS.release(secret)


def active_secret_count() -> int:
    """How many distinct secrets are currently registered. For tests and Doctor."""

    return _SECRETS.count()


def clear_registered_secrets() -> None:
    _SECRETS.clear()


def redact(text: str) -> str:
    """Return ``text`` with secret-looking substrings replaced by ``[REDACTED]``."""

    if not text:
        return text
    result = text
    # Exact registered values first (covers prefixless keys the patterns can't match). The snapshot
    # is bound once: it is immutable, so concurrent registration cannot disturb this loop.
    for secret in _SECRETS.snapshot():
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
