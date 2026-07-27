"""Retry policy (spec §8.3).

Pure decision logic, kept out of :mod:`.transport` so it can be tested without a socket and reused
by anything else that talks to a provider.

The rule the rest of this module exists to enforce: a retry budget is measured in *wall clock*, not
in attempts. Counting attempts alone produces a request that advertises a 120-second timeout and
then takes eight minutes — three attempts at 120 seconds each plus 1+2+4 seconds of backoff — which
is how a "timeout" becomes something the user reports as a hang. The deadline is fixed once, when
the call starts, and every attempt and every sleep is spent from it.

Backoff is exponential, capped, and jittered. The jitter is not decoration: when a provider returns
429 to a fleet of clients, undithered exponential backoff synchronizes them into retrying in the
same instant, and the second wave fails for the same reason as the first.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..core.errors import ErrorType, is_retryable

#: HTTP statuses worth sending the identical request again for (spec §8.3).
#:
#: 500 is included alongside the spec's 429/502/503/504. Model gateways return it for transient
#: upstream capacity failures about as often as for genuine server bugs, and the spec's list is a
#: floor rather than a closed set — it names what *must* be retryable and what must not, and 500 is
#: in neither. A caller that disagrees passes its own set.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Statuses spec §8.3 forbids retrying. Asserted against :data:`RETRYABLE_STATUSES` in the tests so
#: the two cannot drift into overlap.
NEVER_RETRY_STATUSES = frozenset({400, 401, 402, 403, 404})

#: Headers providers use to identify a request. Recorded on failure so a user can quote it to the
#: provider's support; it is an opaque correlation id, not a credential.
REQUEST_ID_HEADERS = (
    "x-request-id",
    "request-id",
    "x-amzn-requestid",
    "x-ms-request-id",
    "openai-request-id",
    "cf-ray",
)


@dataclass(frozen=True)
class RetryPolicy:
    """How many times, how long between, and how long in total (spec §8.3)."""

    max_retries: int = 3
    backoff_base: float = 1.0
    backoff_max: float = 30.0
    #: Wall-clock ceiling across every attempt *and* every backoff sleep.
    total_budget: float = 120.0
    #: Equal jitter — half the computed delay, plus up to half again at random.
    jitter: bool = True

    def delay_for(
        self,
        attempt: int,
        *,
        retry_after: float | None = None,
        rand: Callable[[], float] = random.random,
    ) -> float:
        """Seconds to wait before ``attempt + 1``.

        A provider-supplied ``Retry-After`` wins outright and is used unjittered: the provider has
        told us when it will serve us again, and dithering that is second-guessing an authoritative
        answer with a guess. It is still capped — a header asking for an hour is not a reason to
        hold a run open for an hour.
        """

        if retry_after is not None:
            return max(0.0, min(self.backoff_max, retry_after))
        delay = min(self.backoff_max, self.backoff_base * (2 ** max(0, attempt)))
        if self.jitter and delay > 0:
            delay = delay / 2 + delay / 2 * rand()
        return delay


DEFAULT_RETRY_POLICY = RetryPolicy()


class RetryBudget:
    """The wall clock for one logical call, shared by all of its attempts.

    Created once per call rather than per attempt. That is the entire point: a budget recreated on
    each attempt is not a budget.
    """

    def __init__(self, total: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._deadline = clock() + max(0.0, total)

    @property
    def remaining(self) -> float:
        return max(0.0, self._deadline - self._clock())

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0.0

    def allows(self, delay: float) -> bool:
        """Whether sleeping ``delay`` would still leave time to make the attempt after it.

        Sleeping right up to the deadline and then timing out immediately wastes the wait and
        reports a timeout that the wait itself caused, so a sleep that would consume the remainder
        is refused and the original error surfaces now.
        """

        return delay < self.remaining

    def clamp(self, timeout: float) -> float:
        """Cap a per-attempt timeout so no single attempt can outlive the budget."""

        return min(timeout, self.remaining)


def should_retry(
    *,
    error_type: ErrorType,
    attempt: int,
    policy: RetryPolicy,
    budget: RetryBudget,
    delay: float,
    already_streamed: bool = False,
) -> bool:
    """The single place that decides whether to send the identical request again.

    ``already_streamed`` is the override that outranks every other consideration: once any event
    has reached the caller, replaying duplicates its text, its tool calls and any file changes they
    made. That is not a slower failure, it is a wrong result, so it is refused regardless of how
    retryable the error looks or how much budget is left (spec §44).
    """

    if already_streamed:
        return False
    if attempt >= policy.max_retries:
        return False
    # Deliberately delegated rather than restated: a second list here would be a second place to
    # forget, and the two would disagree exactly once, quietly, in whichever one nobody edited.
    if not is_retryable(error_type):
        return False
    return budget.allows(delay)


def extract_request_id(headers: Mapping[str, str] | object) -> str | None:
    """Pull a provider request id out of response headers, if there is one.

    Returns ``None`` rather than a placeholder: an absent correlation id and an id we failed to
    read are the same thing to the user, and inventing one would make a support conversation worse.
    """

    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    for name in REQUEST_ID_HEADERS:
        value = getter(name)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return None
