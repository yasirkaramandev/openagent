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
        """POST and yield decoded SSE ``data:`` JSON objects (retries only before first byte)."""

        attempt = 0
        while True:
            try:
                async with self.client().stream("POST", path, json=payload) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        retry_after = _retry_after(response)
                        if response.status_code in _RETRY_STATUSES and attempt < self.max_retries:
                            await self._sleep(attempt, retry_after)
                            attempt += 1
                            break  # retry outer loop
                        raise TransportError(
                            classify_http_status(response.status_code), body,
                            status=response.status_code,
                        )
                    async for line in response.aiter_lines():
                        obj = _parse_sse_line(line)
                        if obj is _DONE:
                            return
                        if obj is not None:
                            yield obj
                    return
            except (httpx.TimeoutException, httpx.TransportError) as exc:
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


_DONE = object()


def _parse_sse_line(line: str) -> dict[str, Any] | object | None:
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if line.startswith("data:"):
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            return _DONE
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None
    return None


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
