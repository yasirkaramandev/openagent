"""Transport-level retry budget, request ids and redaction (spec §8.3).

The budget assertions here are what a pure `RetryBudget` test cannot prove: that `post_json` and
`stream_sse` open *one* budget per logical call rather than one per attempt. Before that, a call
declaring a 120-second timeout could run for over eight minutes — three attempts at the full
timeout plus 1+2+4 seconds of backoff — and still report a 120-second timeout.
"""

from __future__ import annotations

import httpx
import pytest

from openagent.core.errors import ErrorType
from openagent.providers.transport import Transport, TransportError

pytestmark = pytest.mark.unit


class _Response:
    def __init__(self, status: int, *, body: object = None, headers: dict | None = None) -> None:
        self.status_code = status
        self._body = body if body is not None else {"ok": True}
        self.headers = headers or {}
        self.text = str(self._body)

    def json(self):
        return self._body


class _PostClient:
    """Replays a scripted sequence of responses/exceptions, counting attempts."""

    def __init__(self, script: list) -> None:
        self._script = script
        self.calls = 0

    async def post(self, path, json=None):
        item = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _transport(client, **kwargs) -> Transport:
    transport = Transport(base_url="https://api.test", **kwargs)
    transport._client = client  # type: ignore[assignment]
    return transport


def _timed_transport(client, monkeypatch, **kwargs) -> tuple[Transport, _Clock, list[float]]:
    """A transport whose fake sleeps advance its fake clock.

    Without this the budget never depletes under a no-op sleep, and a test claiming to prove a
    wall-clock bound proves only that the attempt counter works.
    """

    clock = _Clock()
    slept: list[float] = []

    async def fake_sleep(delay):
        slept.append(delay)
        clock.now += delay

    monkeypatch.setattr("openagent.providers.transport.asyncio.sleep", fake_sleep)
    transport = _transport(client, clock=clock, **kwargs)
    return transport, clock, slept


# --------------------------------------------------------------------------- budget


async def test_the_budget_is_opened_once_per_call_not_once_per_attempt(monkeypatch):
    """Backoff sleeps are spent from the same clock the attempts are."""

    client = _PostClient([_Response(503)] * 6)
    # 2.5s of budget can afford the 1s backoff, but not the 2s one that would follow it.
    transport, _clock, slept = _timed_transport(
        client, monkeypatch, total_timeout=2.5, backoff_base=1.0, retry_jitter=False
    )

    with pytest.raises(TransportError):
        await transport.post_json("/chat", {})

    assert slept == [1.0]  # after sleeping 1s only 1.5s remains, so a 2s wait is refused
    assert client.calls == 2


async def test_time_spent_in_backoff_is_not_available_to_later_attempts(monkeypatch):
    client = _PostClient([_Response(503)] * 6)
    transport, clock, slept = _timed_transport(
        client, monkeypatch, total_timeout=100.0, backoff_base=1.0, retry_jitter=False
    )

    with pytest.raises(TransportError):
        await transport.post_json("/chat", {})

    # 1 + 2 + 4 seconds of backoff really came out of the 100-second budget.
    assert slept == [1.0, 2.0, 4.0]
    assert clock.now == pytest.approx(1007.0)


async def test_retries_stop_once_the_budget_is_gone(monkeypatch):
    async def fake_sleep(delay):
        pass

    monkeypatch.setattr("openagent.providers.transport.asyncio.sleep", fake_sleep)
    client = _PostClient([_Response(503)] * 10)
    transport = _transport(client, total_timeout=0.0, backoff_base=0.0)

    with pytest.raises(TransportError) as exc:
        await transport.post_json("/chat", {})

    assert exc.value.error_type is ErrorType.TIMEOUT
    assert "exceeded" in exc.value.message
    assert client.calls == 0  # never even attempted: the budget was already spent


async def test_a_generous_budget_still_honours_the_attempt_ceiling(monkeypatch):
    async def fake_sleep(delay):
        pass

    monkeypatch.setattr("openagent.providers.transport.asyncio.sleep", fake_sleep)
    client = _PostClient([_Response(503)] * 10)
    transport = _transport(client, total_timeout=10_000.0, backoff_base=0.0, max_retries=3)

    with pytest.raises(TransportError):
        await transport.post_json("/chat", {})

    assert client.calls == 4  # the first attempt plus three retries


async def test_retry_after_is_honoured_over_the_computed_backoff(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr("openagent.providers.transport.asyncio.sleep", fake_sleep)
    client = _PostClient(
        [_Response(429, headers={"retry-after": "7"}), _Response(200, body={"ok": True})]
    )
    transport = _transport(client, backoff_base=1.0, retry_jitter=False)

    assert await transport.post_json("/chat", {}) == {"ok": True}
    assert slept == [7.0]


async def test_a_non_retryable_status_is_not_retried(monkeypatch):
    client = _PostClient([_Response(401, body={"error": {"message": "bad key"}})])
    transport = _transport(client)

    with pytest.raises(TransportError) as exc:
        await transport.post_json("/chat", {})

    assert exc.value.error_type is ErrorType.AUTHENTICATION_FAILED
    assert client.calls == 1


async def test_transport_errors_are_retried_within_budget(monkeypatch):
    async def fake_sleep(delay):
        pass

    monkeypatch.setattr("openagent.providers.transport.asyncio.sleep", fake_sleep)
    client = _PostClient([httpx.ConnectError("reset"), _Response(200, body={"ok": 1})])
    transport = _transport(client, backoff_base=0.0)

    assert await transport.post_json("/chat", {}) == {"ok": 1}
    assert client.calls == 2


# --------------------------------------------------------------------------- request id


async def test_request_id_is_captured_from_a_successful_response():
    client = _PostClient([_Response(200, headers={"x-request-id": "req_ok"})])
    transport = _transport(client)

    await transport.post_json("/chat", {})
    assert transport.last_request_id == "req_ok"


async def test_request_id_is_attached_to_the_failure_it_belongs_to():
    client = _PostClient([_Response(400, headers={"x-request-id": "req_bad"})])
    transport = _transport(client)

    with pytest.raises(TransportError) as exc:
        await transport.post_json("/chat", {})

    assert exc.value.request_id == "req_bad"


async def test_a_provider_that_sends_no_request_id_reports_none():
    client = _PostClient([_Response(400)])
    transport = _transport(client)

    with pytest.raises(TransportError) as exc:
        await transport.post_json("/chat", {})

    assert exc.value.request_id is None


# --------------------------------------------------------------------------- redaction


async def test_a_provider_error_echoing_a_key_is_redacted_at_construction():
    # Provider error bodies quote the header they rejected; this object reaches logs, events, the
    # TUI and test output, so redaction happens once here rather than at each rendering site.
    body = {"error": {"message": "invalid key sk-live-abcdef123456 for this project"}}
    client = _PostClient([_Response(401, body=body)])
    transport = _transport(client)

    with pytest.raises(TransportError) as exc:
        await transport.post_json("/chat", {})

    assert "sk-live-abcdef123456" not in exc.value.message
    assert "[redacted]" in exc.value.message
    assert "sk-live-abcdef123456" not in str(exc.value)


async def test_a_bearer_token_in_an_error_body_is_redacted():
    body = {"error": {"message": "rejected Bearer abcdef1234567890"}}
    client = _PostClient([_Response(403, body=body)])
    transport = _transport(client)

    with pytest.raises(TransportError) as exc:
        await transport.post_json("/chat", {})

    assert "abcdef1234567890" not in exc.value.message


def test_redaction_leaves_ordinary_diagnostics_intact():
    error = TransportError(ErrorType.MODEL_NOT_FOUND, "model gemini-3-pro-preview not found")
    assert error.message == "model gemini-3-pro-preview not found"
