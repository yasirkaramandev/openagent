"""Shared HTTP transport for all providers (spec §44).

Centralizes: auth headers, JSON POSTs, SSE streaming, and retry/backoff. Only *safe* errors are
retried (429, 502/503/504, timeouts, connection resets) with exponential backoff (1/2/4/8s), and a
provider-supplied ``Retry-After`` takes precedence.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..core.cancellation import RunCancellation
from ..core.errors import ErrorType, classify_http_status, is_retryable
from ..core.limits import RUNTIME_LIMITS

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
    timeout: float | None = None
    total_timeout: float = 120.0
    max_retries: int = 3
    backoff_base: float = 1.0
    cancellation: RunCancellation | None = field(default=None, repr=False)
    _client: httpx.AsyncClient | None = field(default=None, repr=False)

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = (
                httpx.Timeout(self.timeout)
                if self.timeout is not None
                else httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
            )
            self._client = httpx.AsyncClient(
                base_url=self.base_url, headers=self.headers, timeout=timeout
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
                response = await asyncio.wait_for(
                    self.client().post(path, json=payload), timeout=self.total_timeout
                )
            except (
                TimeoutError,
                asyncio.TimeoutError,
                httpx.TimeoutException,
                httpx.TransportError,
            ) as exc:
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
            try:
                data = response.json()
            except ValueError as exc:
                raise TransportError(
                    ErrorType.INVALID_REQUEST, "provider returned invalid JSON"
                ) from exc
            if not isinstance(data, dict):
                raise TransportError(
                    ErrorType.INVALID_REQUEST, "provider returned a non-object JSON body"
                )
            return data

    async def stream_sse(self, path: str, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """POST and yield decoded SSE ``data:`` JSON objects.

        A stream is only safe to replay **before the first event is yielded**. Once any event has
        been delivered to the caller, a mid-stream disconnect would duplicate text, tool calls, and
        file changes on replay — so we do not retry; we raise ``CONNECTION_LOST`` and let the caller
        surface a clear error (spec §44). Retries before the first event use exponential backoff.
        """

        attempt = 0
        received_event = False
        malformed = 0
        data_lines = 0
        deadline = time.monotonic() + self.total_timeout
        while True:
            try:
                async with self.client().stream("POST", path, json=payload) as response:
                    if response.status_code == 202:
                        await response.aread()
                        raise TransportError(
                            ErrorType.ASYNC_UNSUPPORTED, _ASYNC_MESSAGE, status=202
                        )
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        retry_after = _retry_after(response)
                        # Header errors arrive before any event, so retrying is still safe.
                        if (
                            response.status_code in _RETRY_STATUSES
                            and attempt < self.max_retries
                            and not received_event
                        ):
                            await self._sleep(attempt, retry_after)
                            attempt += 1
                            continue  # retry outer loop
                        raise TransportError(
                            classify_http_status(response.status_code),
                            body,
                            status=response.status_code,
                        )
                    async for line in response.aiter_lines():
                        if time.monotonic() >= deadline:
                            raise TransportError(
                                ErrorType.TIMEOUT, "provider stream exceeded 120 seconds"
                            )
                        stripped = line.strip()
                        if (
                            not stripped
                            or stripped.startswith(":")
                            or not stripped.startswith("data:")
                        ):
                            continue
                        payload_str = stripped[len("data:") :].strip()
                        data_lines += 1
                        if payload_str == "[DONE]":
                            return
                        try:
                            obj = json.loads(payload_str)
                        except json.JSONDecodeError:
                            malformed += 1
                            continue
                        if isinstance(obj, dict):
                            if obj.get("error"):
                                raise TransportError(
                                    ErrorType.UNKNOWN,
                                    str(obj.get("error"))[: RUNTIME_LIMITS.provider_error_bytes],
                                )
                            received_event = True
                            yield obj
                        else:
                            malformed += 1
                    if data_lines and malformed == data_lines and not received_event:
                        raise TransportError(
                            ErrorType.MALFORMED_STREAM,
                            "provider stream contained only malformed data events",
                        )
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
        try:
            response = await asyncio.wait_for(self.client().get(path), timeout=self.total_timeout)
        except (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException) as exc:
            raise TransportError(ErrorType.TIMEOUT, "provider request timed out") from exc
        except httpx.TransportError as exc:
            # Keep URLs and proxy diagnostics out of the durable/user-visible message; discovery
            # still distinguishes this from a timeout via CONNECTION_LOST.
            raise TransportError(
                ErrorType.CONNECTION_LOST, "provider network request failed"
            ) from exc
        if response.status_code >= 400:
            raise TransportError(
                classify_http_status(response.status_code),
                _error_text(response),
                status=response.status_code,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise TransportError(
                ErrorType.INVALID_REQUEST, "provider returned invalid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise TransportError(
                ErrorType.INVALID_REQUEST, "provider returned a non-object JSON body"
            )
        return data

    async def _sleep(self, attempt: int, retry_after: float | None) -> None:
        delay = retry_after if retry_after is not None else self.backoff_base * (2**attempt)
        delay = min(30.0, max(0.0, delay))
        if self.cancellation is not None:
            await self.cancellation.guard(asyncio.sleep(delay))
        else:
            await asyncio.sleep(delay)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return min(parsed, 30.0)


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                return str(err.get("message", data))[: RUNTIME_LIMITS.provider_error_bytes]
            return str(err or data.get("message", data))[: RUNTIME_LIMITS.provider_error_bytes]
    except Exception:  # pragma: no cover - non-JSON error body
        pass
    return response.text[: RUNTIME_LIMITS.provider_error_bytes]


def retryable_error(error_type: ErrorType) -> bool:
    return is_retryable(error_type)
