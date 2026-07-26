"""Retry policy (spec §8.3).

The budget tests are the ones that matter. Attempt-counting alone was already correct before this
module existed; what was missing was a wall clock, and its absence is invisible in any test that
only counts attempts.
"""

from __future__ import annotations

import pytest

from openagent.core.errors import ErrorType
from openagent.providers.retry import (
    NEVER_RETRY_STATUSES,
    RETRYABLE_STATUSES,
    RetryBudget,
    RetryPolicy,
    extract_request_id,
    should_retry,
)

pytestmark = pytest.mark.unit


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --------------------------------------------------------------------------- backoff


def test_backoff_is_exponential_from_the_base():
    policy = RetryPolicy(backoff_base=1.0, jitter=False)
    assert [policy.delay_for(i) for i in range(4)] == [1.0, 2.0, 4.0, 8.0]


def test_backoff_is_capped():
    policy = RetryPolicy(backoff_base=1.0, backoff_max=5.0, jitter=False)
    assert policy.delay_for(10) == 5.0


def test_equal_jitter_stays_within_half_and_full_delay():
    policy = RetryPolicy(backoff_base=4.0, jitter=True)
    assert policy.delay_for(0, rand=lambda: 0.0) == 2.0
    assert policy.delay_for(0, rand=lambda: 1.0) == 4.0
    assert policy.delay_for(0, rand=lambda: 0.5) == 3.0


def test_jitter_never_produces_a_delay_above_the_cap():
    policy = RetryPolicy(backoff_base=100.0, backoff_max=30.0, jitter=True)
    assert policy.delay_for(5, rand=lambda: 1.0) == 30.0


def test_a_zero_base_disables_backoff_entirely_even_with_jitter():
    policy = RetryPolicy(backoff_base=0.0, jitter=True)
    assert policy.delay_for(3, rand=lambda: 1.0) == 0.0


def test_retry_after_overrides_the_computed_backoff_and_is_not_jittered():
    policy = RetryPolicy(backoff_base=1.0, jitter=True)
    # The provider said when it will serve us; dithering that is second-guessing an authority.
    assert policy.delay_for(5, retry_after=7.0, rand=lambda: 0.0) == 7.0


def test_retry_after_is_still_capped():
    policy = RetryPolicy(backoff_max=30.0)
    assert policy.delay_for(0, retry_after=3600.0) == 30.0


def test_negative_retry_after_is_floored_at_zero():
    assert RetryPolicy().delay_for(0, retry_after=-5.0) == 0.0


# --------------------------------------------------------------------------- budget


def test_budget_counts_down_in_wall_clock():
    clock = _Clock()
    budget = RetryBudget(10.0, clock=clock)
    assert budget.remaining == 10.0
    clock.advance(4.0)
    assert budget.remaining == 6.0


def test_budget_is_exhausted_at_the_deadline():
    clock = _Clock()
    budget = RetryBudget(10.0, clock=clock)
    clock.advance(10.0)
    assert budget.exhausted
    assert budget.remaining == 0.0


def test_budget_never_reports_negative_remaining():
    clock = _Clock()
    budget = RetryBudget(1.0, clock=clock)
    clock.advance(100.0)
    assert budget.remaining == 0.0


def test_budget_refuses_a_sleep_that_would_consume_the_remainder():
    # Sleeping to the deadline and then timing out reports a timeout the sleep itself caused.
    clock = _Clock()
    budget = RetryBudget(10.0, clock=clock)
    clock.advance(8.0)
    assert budget.allows(1.0)
    assert not budget.allows(2.0)
    assert not budget.allows(5.0)


def test_budget_clamps_a_per_attempt_timeout_so_no_attempt_outlives_it():
    clock = _Clock()
    budget = RetryBudget(10.0, clock=clock)
    clock.advance(7.0)
    assert budget.clamp(120.0) == 3.0


def test_budget_clamp_leaves_a_shorter_timeout_alone():
    budget = RetryBudget(100.0, clock=_Clock())
    assert budget.clamp(5.0) == 5.0


# --------------------------------------------------------------------------- should_retry


def _budget(remaining: float = 1000.0) -> RetryBudget:
    return RetryBudget(remaining, clock=_Clock())


@pytest.mark.parametrize(
    "error_type",
    [ErrorType.PROVIDER_RATE_LIMITED, ErrorType.PROVIDER_OVERLOADED, ErrorType.TIMEOUT],
)
def test_transient_errors_are_retried(error_type: ErrorType):
    assert should_retry(
        error_type=error_type, attempt=0, policy=RetryPolicy(), budget=_budget(), delay=1.0
    )


@pytest.mark.parametrize(
    "error_type",
    [
        ErrorType.AUTHENTICATION_FAILED,
        ErrorType.INSUFFICIENT_BALANCE,
        ErrorType.MODEL_NOT_FOUND,
        ErrorType.CONTEXT_LIMIT,
        ErrorType.INVALID_TOOL_ARGUMENTS,
        ErrorType.LOCAL_SERVER_UNAVAILABLE,
    ],
)
def test_settled_failures_are_not_retried(error_type: ErrorType):
    assert not should_retry(
        error_type=error_type, attempt=0, policy=RetryPolicy(), budget=_budget(), delay=1.0
    )


def test_retries_stop_at_the_attempt_ceiling():
    policy = RetryPolicy(max_retries=2)
    common = {
        "error_type": ErrorType.PROVIDER_OVERLOADED,
        "policy": policy,
        "budget": _budget(),
        "delay": 1.0,
    }
    assert should_retry(attempt=0, **common)
    assert should_retry(attempt=1, **common)
    assert not should_retry(attempt=2, **common)


def test_retries_stop_when_the_budget_cannot_afford_the_wait():
    clock = _Clock()
    budget = RetryBudget(10.0, clock=clock)
    clock.advance(9.5)
    assert not should_retry(
        error_type=ErrorType.PROVIDER_OVERLOADED,
        attempt=0,
        policy=RetryPolicy(),
        budget=budget,
        delay=1.0,
    )


def test_a_stream_that_already_delivered_events_is_never_retried():
    # Outranks everything: replaying duplicates text, tool calls and the file changes they made.
    assert not should_retry(
        error_type=ErrorType.PROVIDER_OVERLOADED,
        attempt=0,
        policy=RetryPolicy(max_retries=99),
        budget=_budget(),
        delay=0.0,
        already_streamed=True,
    )


# --------------------------------------------------------------------------- status sets


def test_retryable_and_never_retry_statuses_do_not_overlap():
    assert RETRYABLE_STATUSES & NEVER_RETRY_STATUSES == frozenset()


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_spec_retryable_statuses_are_present(status: int):
    assert status in RETRYABLE_STATUSES


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404])
def test_spec_forbidden_statuses_are_never_retried(status: int):
    assert status not in RETRYABLE_STATUSES


# --------------------------------------------------------------------------- request id


@pytest.mark.parametrize(
    "header",
    ["x-request-id", "request-id", "openai-request-id", "x-amzn-requestid", "cf-ray"],
)
def test_request_id_is_read_from_each_known_header(header: str):
    assert extract_request_id({header: "req_abc123"}) == "req_abc123"


def test_absent_request_id_is_none_rather_than_a_placeholder():
    assert extract_request_id({}) is None
    assert extract_request_id({"x-request-id": "   "}) is None
    assert extract_request_id(None) is None


def test_request_id_is_bounded():
    assert len(extract_request_id({"x-request-id": "x" * 5000}) or "") == 200
