"""Shared HTTP transport for all providers (spec §44).

Centralizes: auth headers, JSON POSTs, SSE streaming, and retry/backoff. Only *safe* errors are
retried (429, 502/503/504, timeouts, connection resets) with exponential backoff (1/2/4/8s), and a
provider-supplied ``Retry-After`` takes precedence.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..core.errors import ErrorType, classify_http_status, is_retryable

_RETRY_STATUSES = {429, 500, 502, 503, 504}

#: A 202 means the provider queued the request and expects the caller to poll for a result. The chat
#: runtime is synchronous and has no polling, so a 202 is an explicit, honest failure — never an empty
#: "success" with no content (spec §15.5; some NVIDIA model types behave this way).
_ASYNC_MESSAGE = (
    "Asynchronous NVIDIA invocation is not supported by the OpenAgent chat runtime yet "
    "(the endpoint returned HTTP 202 with a request id instead of a completion)."
)


class TransportError(Exception):
    def __init__(self, error_type: ErrorType, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.status = status


@dataclass
class Transport:
    base_url: str
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 120.0
    max_retries: int = 4
    backoff_base: float = 1.0
    _client: httpx.AsyncClient | None = field(default=None, repr=False)

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url, headers=self.headers, timeout=self.timeout
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> Transport:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ requests

    async def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST JSON with retry; returns parsed JSON or raises :class:`TransportError`."""

        attempt = 0
        while True:
            try:
                response = await self.client().post(path, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self.max_retries:
                    raise TransportError(ErrorType.TIMEOUT, str(exc)) from exc
                await self._sleep(attempt, None)
                attempt += 1
                continue

            if response.status_code == 202:
                raise TransportError(ErrorType.ASYNC_UNSUPPORTED, _ASYNC_MESSAGE, status=202)
            if response.status_code >= 400:
                retry_after = _retry_after(response)
                if response.status_code in _RETRY_STATUSES and attempt < self.max_retries:
                    await self._sleep(attempt, retry_after)
                    attempt += 1
                    continue
                raise TransportError(
                    classify_http_status(response.status_code),
                    _error_text(response),
                    status=response.status_code,
                )
            return response.json()

    async def stream_sse(self, path: str, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """POST and yield decoded SSE ``data:`` JSON objects.

        A stream is only safe to replay **before the first event is yielded**. Once any event has
        been delivered to the caller, a mid-stream disconnect would duplicate text, tool calls, and
        file changes on replay — so we do not retry; we raise ``CONNECTION_LOST`` and let the caller
        surface a clear error (spec §44). Retries before the first event use exponential backoff.
        """

        attempt = 0
        received_event = False
        while True:
            try:
                async with self.client().stream("POST", path, json=payload) as response:
                    if response.status_code == 202:
                        await response.aread()
                        raise TransportError(ErrorType.ASYNC_UNSUPPORTED, _ASYNC_MESSAGE, status=202)
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        retry_after = _retry_after(response)
                        # Header errors arrive before any event, so retrying is still safe.
                        if (response.status_code in _RETRY_STATUSES and attempt < self.max_retries
                                and not received_event):
                            await self._sleep(attempt, retry_after)
                            attempt += 1
                            continue  # retry outer loop
                        raise TransportError(
                            classify_http_status(response.status_code), body,
                            status=response.status_code,
                        )
                    async for line in response.aiter_lines():
                        stripped = line.strip()
                        if not stripped or stripped.startswith(":") or not stripped.startswith("data:"):
                            continue
                        payload_str = stripped[len("data:"):].strip()
                        if payload_str == "[DONE]":
                            return
                        try:
                            obj = json.loads(payload_str)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(obj, dict):
                            received_event = True
                            yield obj
                    return
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # If we already yielded events, replaying the request would double-apply its effects.
                if received_event:
                    raise TransportError(
                        ErrorType.CONNECTION_LOST,
                        f"stream disconnected after partial output; not retried ({exc})",
                    ) from exc
                if attempt >= self.max_retries:
                    raise TransportError(ErrorType.TIMEOUT, str(exc)) from exc
                await self._sleep(attempt, None)
                attempt += 1

    async def get_json(self, path: str) -> dict[str, Any]:
        response = await self.client().get(path)
        if response.status_code >= 400:
            raise TransportError(
                classify_http_status(response.status_code), _error_text(response),
                status=response.status_code,
            )
        return response.json()

    async def _sleep(self, attempt: int, retry_after: float | None) -> None:
        delay = retry_after if retry_after is not None else self.backoff_base * (2**attempt)
        await asyncio.sleep(delay)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                return str(err.get("message", data))
            return str(err or data.get("message", data))
    except Exception:  # pragma: no cover - non-JSON error body
        pass
    return response.text[:500]


def retryable_error(error_type: ErrorType) -> bool:
    return is_retryable(error_type)
