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
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..core.cancellation import RunCancellation
from ..core.errors import ErrorType, classify_http_status, is_retryable, redact_secrets
from ..core.limits import RUNTIME_LIMITS
from .retry import RETRYABLE_STATUSES, RetryBudget, RetryPolicy, extract_request_id

_RETRY_STATUSES = RETRYABLE_STATUSES

#: A 202 means the provider queued the request and expects the caller to poll for a result. The chat
#: runtime is synchronous and has no polling, so a 202 is an explicit, honest failure — never an empty
#: "success" with no content (spec §15.5; some NVIDIA model types behave this way).
_ASYNC_MESSAGE = (
    "Asynchronous NVIDIA invocation is not supported by the OpenAgent chat runtime yet "
    "(the endpoint returned HTTP 202 with a request id instead of a completion)."
)


class TransportError(Exception):
    """A provider call that failed, carrying everything a user needs to act on it.

    ``message`` is redacted at construction rather than at the point it is rendered. A provider
    error body can echo the ``Authorization`` header it rejected, and this object is passed to
    logs, events, the TUI and test output — redacting once, here, means no future caller can add a
    fifth rendering path that forgets.
    """

    def __init__(
        self,
        error_type: ErrorType,
        message: str,
        status: int | None = None,
        *,
        request_id: str | None = None,
    ) -> None:
        message = redact_secrets(message)
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.status = status
        #: The provider's own correlation id, when it sent one. Opaque, quotable to their support.
        self.request_id = request_id


@dataclass
class Transport:
    base_url: str
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float | None = None
    #: Wall-clock ceiling for one logical call — *all* of its attempts and backoff sleeps, not each
    #: attempt separately. Before this was a shared budget, three retries of a 120s call could run
    #: for over eight minutes and still be reported as a 120-second timeout (spec §8.3).
    total_timeout: float = 120.0
    max_retries: int = 3
    backoff_base: float = 1.0
    backoff_max: float = 30.0
    #: Equal jitter on backoff, so a fleet rate-limited at the same moment does not retry in unison.
    retry_jitter: bool = True
    cancellation: RunCancellation | None = field(default=None, repr=False)
    #: The provider correlation id from the most recent response, for diagnostics.
    last_request_id: str | None = field(default=None, repr=False)
    #: Injectable so the wall-clock budget is testable without spending real seconds. A test that
    #: has to sleep 8 real seconds to prove a backoff bound does not get written, and the bound then
    #: goes unverified.
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    _client: httpx.AsyncClient | None = field(default=None, repr=False)

    def _budget(self) -> RetryBudget:
        return RetryBudget(self.total_timeout, clock=self.clock)

    def _policy(self) -> RetryPolicy:
        """Read the live fields each call, so a caller may retune a transport after construction."""

        return RetryPolicy(
            max_retries=self.max_retries,
            backoff_base=self.backoff_base,
            backoff_max=self.backoff_max,
            total_budget=self.total_timeout,
            jitter=self.retry_jitter,
        )

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
        """POST JSON with retry; returns parsed JSON or raises :class:`TransportError`.

        Every attempt and every backoff sleep is spent from one budget opened here, so the call
        cannot outlive ``total_timeout`` no matter how the retries fall.
        """

        policy = self._policy()
        budget = self._budget()
        attempt = 0
        while True:
            if budget.exhausted:
                raise TransportError(
                    ErrorType.TIMEOUT, f"provider request exceeded {policy.total_budget:g} seconds"
                )
            try:
                response = await asyncio.wait_for(
                    self.client().post(path, json=payload),
                    timeout=budget.clamp(policy.total_budget),
                )
            except (
                TimeoutError,
                asyncio.TimeoutError,
                httpx.TimeoutException,
                httpx.TransportError,
            ) as exc:
                delay = policy.delay_for(attempt)
                if attempt >= policy.max_retries or not budget.allows(delay):
                    raise TransportError(ErrorType.TIMEOUT, str(exc)) from exc
                await self._sleep(delay)
                attempt += 1
                continue

            self.last_request_id = extract_request_id(response.headers)
            if response.status_code == 202:
                raise TransportError(
                    ErrorType.ASYNC_UNSUPPORTED,
                    _ASYNC_MESSAGE,
                    status=202,
                    request_id=self.last_request_id,
                )
            if response.status_code >= 400:
                retry_after = _retry_after(response)
                delay = policy.delay_for(attempt, retry_after=retry_after)
                if (
                    response.status_code in _RETRY_STATUSES
                    and attempt < policy.max_retries
                    and budget.allows(delay)
                ):
                    await self._sleep(delay)
                    attempt += 1
                    continue
                raise TransportError(
                    classify_http_status(response.status_code),
                    _error_text(response),
                    status=response.status_code,
                    request_id=self.last_request_id,
                )
            try:
                data = response.json()
            except ValueError as exc:
                raise TransportError(
                    ErrorType.INVALID_REQUEST,
                    "provider returned invalid JSON",
                    request_id=self.last_request_id,
                ) from exc
            if not isinstance(data, dict):
                raise TransportError(
                    ErrorType.INVALID_REQUEST,
                    "provider returned a non-object JSON body",
                    request_id=self.last_request_id,
                )
            return data

    async def stream_sse(self, path: str, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """POST and yield decoded SSE ``data:`` JSON objects.

        A stream is only safe to replay **before the first event is yielded**. Once any event has
        been delivered to the caller, a mid-stream disconnect would duplicate text, tool calls, and
        file changes on replay — so we do not retry; we raise ``CONNECTION_LOST`` and let the caller
        surface a clear error (spec §44). Retries before the first event use exponential backoff.
        """

        policy = self._policy()
        budget = self._budget()
        attempt = 0
        received_event = False
        malformed = 0
        data_lines = 0
        while True:
            try:
                async with self.client().stream("POST", path, json=payload) as response:
                    self.last_request_id = extract_request_id(response.headers)
                    if response.status_code == 202:
                        await response.aread()
                        raise TransportError(
                            ErrorType.ASYNC_UNSUPPORTED,
                            _ASYNC_MESSAGE,
                            status=202,
                            request_id=self.last_request_id,
                        )
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        retry_after = _retry_after(response)
                        delay = policy.delay_for(attempt, retry_after=retry_after)
                        # Header errors arrive before any event, so retrying is still safe.
                        if (
                            response.status_code in _RETRY_STATUSES
                            and attempt < policy.max_retries
                            and not received_event
                            and budget.allows(delay)
                        ):
                            await self._sleep(delay)
                            attempt += 1
                            continue  # retry outer loop
                        raise TransportError(
                            classify_http_status(response.status_code),
                            body,
                            status=response.status_code,
                            request_id=self.last_request_id,
                        )
                    async for line in response.aiter_lines():
                        if budget.exhausted:
                            raise TransportError(
                                ErrorType.TIMEOUT,
                                f"provider stream exceeded {policy.total_budget:g} seconds",
                                request_id=self.last_request_id,
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
                                    request_id=self.last_request_id,
                                )
                            received_event = True
                            yield obj
                        else:
                            malformed += 1
                    if data_lines and malformed == data_lines and not received_event:
                        raise TransportError(
                            ErrorType.MALFORMED_STREAM,
                            "provider stream contained only malformed data events",
                            request_id=self.last_request_id,
                        )
                    return
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # If we already yielded events, replaying the request would double-apply its effects.
                if received_event:
                    raise TransportError(
                        ErrorType.CONNECTION_LOST,
                        f"stream disconnected after partial output; not retried ({exc})",
                        request_id=self.last_request_id,
                    ) from exc
                delay = policy.delay_for(attempt)
                if attempt >= policy.max_retries or not budget.allows(delay):
                    raise TransportError(
                        ErrorType.TIMEOUT, str(exc), request_id=self.last_request_id
                    ) from exc
                await self._sleep(delay)
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
        self.last_request_id = extract_request_id(response.headers)
        if response.status_code >= 400:
            raise TransportError(
                classify_http_status(response.status_code),
                _error_text(response),
                status=response.status_code,
                request_id=self.last_request_id,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise TransportError(
                ErrorType.INVALID_REQUEST,
                "provider returned invalid JSON",
                request_id=self.last_request_id,
            ) from exc
        if not isinstance(data, dict):
            raise TransportError(
                ErrorType.INVALID_REQUEST,
                "provider returned a non-object JSON body",
                request_id=self.last_request_id,
            )
        return data

    async def _sleep(self, delay: float) -> None:
        """Sleep, but stay cancellable.

        A backoff is the longest a run sits doing nothing, so an uncancellable one means Ctrl-C
        appears to hang for up to 30 seconds.
        """

        delay = max(0.0, delay)
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
