"""Error taxonomy v2 (spec §8.2, §8.3).

The retry assertions are the point of this file. `is_retryable` answers by membership in
`RETRYABLE`, so `NON_RETRYABLE` cannot change behaviour by itself — its job is to make a future
edit that moves a deterministic failure into the retry set fail here rather than in a user's
billing.
"""

from __future__ import annotations

import pytest

from openagent.core.errors import (
    NON_RETRYABLE,
    RETRYABLE,
    ErrorType,
    classify_http_status,
    is_retryable,
    redact_secrets,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- taxonomy coverage

#: Every type spec §8.2 requires, mapped to the member that carries it. Two of them predate v0.2
#: under a longer name; the taxonomy is satisfied by the existing member rather than by adding a
#: synonym, because two names for one condition is worse than one name that reads oddly.
_REQUIRED_BY_SPEC = {
    "model_not_found": ErrorType.MODEL_NOT_FOUND,
    "model_deprecated": ErrorType.MODEL_DEPRECATED,
    "model_capability_missing": ErrorType.MODEL_CAPABILITY_MISSING,
    "unsupported_parameter": ErrorType.UNSUPPORTED_PARAMETER,
    "invalid_tool_call": ErrorType.INVALID_TOOL_CALL,
    "invalid_tool_arguments": ErrorType.INVALID_TOOL_ARGUMENTS,
    "tool_history_incomplete": ErrorType.TOOL_HISTORY_INCOMPLETE,
    "continuation_invalid": ErrorType.CONTINUATION_INVALID,
    "remote_session_expired": ErrorType.REMOTE_SESSION_EXPIRED,
    "local_server_unavailable": ErrorType.LOCAL_SERVER_UNAVAILABLE,
    "local_model_not_loaded": ErrorType.LOCAL_MODEL_NOT_LOADED,
    "local_model_out_of_memory": ErrorType.LOCAL_MODEL_OUT_OF_MEMORY,
    "provider_region_mismatch": ErrorType.PROVIDER_REGION_MISMATCH,
    "provider_rate_limited": ErrorType.PROVIDER_RATE_LIMITED,
    "provider_overloaded": ErrorType.PROVIDER_OVERLOADED,
    "insufficient_balance": ErrorType.INSUFFICIENT_BALANCE,
    "authentication_failed": ErrorType.AUTHENTICATION_FAILED,
    "context_limit": ErrorType.CONTEXT_LIMIT,
    "output_limit": ErrorType.OUTPUT_LIMIT_EXCEEDED,
    "structured_output_failed": ErrorType.STRUCTURED_OUTPUT_FAILED,
    "stream_interrupted": ErrorType.STREAM_INTERRUPTED,
    "protocol_mismatch": ErrorType.PROTOCOL_MISMATCH,
    "catalog_unavailable": ErrorType.CATALOG_UNAVAILABLE,
    "catalog_partial": ErrorType.CATALOG_PARTIAL,
}


@pytest.mark.parametrize("spec_name", sorted(_REQUIRED_BY_SPEC))
def test_every_spec_error_type_exists(spec_name: str):
    assert isinstance(_REQUIRED_BY_SPEC[spec_name], ErrorType)


def test_error_type_values_are_unique():
    values = [member.value for member in ErrorType]
    assert len(values) == len(set(values))


def test_stream_interrupted_is_not_a_synonym_for_connection_lost():
    # One is the socket dropping; the other is a stream that ended without a terminal event. They
    # get different remediation, so collapsing them would lose the distinction that matters.
    assert ErrorType.STREAM_INTERRUPTED is not ErrorType.CONNECTION_LOST


# --------------------------------------------------------------------------- retry policy


def test_retryable_and_non_retryable_are_disjoint():
    assert RETRYABLE & NON_RETRYABLE == set()


@pytest.mark.parametrize("error_type", sorted(RETRYABLE, key=lambda e: e.value))
def test_retryable_members_report_retryable(error_type: ErrorType):
    assert is_retryable(error_type)


@pytest.mark.parametrize("error_type", sorted(NON_RETRYABLE, key=lambda e: e.value))
def test_non_retryable_members_are_never_retried(error_type: ErrorType):
    assert not is_retryable(error_type)


@pytest.mark.parametrize(
    "error_type",
    [
        ErrorType.MODEL_NOT_FOUND,
        ErrorType.CONTEXT_LIMIT,
        ErrorType.INSUFFICIENT_BALANCE,
        ErrorType.AUTHENTICATION_FAILED,
        ErrorType.PERMISSION_DENIED,
        ErrorType.INVALID_REQUEST,
        ErrorType.INVALID_TOOL_ARGUMENTS,
    ],
)
def test_spec_forbidden_retries_are_declared_non_retryable(error_type: ErrorType):
    """Spec §8.3 names these explicitly as must-not-retry."""

    assert error_type in NON_RETRYABLE


@pytest.mark.parametrize(
    "error_type",
    [
        ErrorType.PROVIDER_RATE_LIMITED,
        ErrorType.PROVIDER_OVERLOADED,
        ErrorType.TIMEOUT,
    ],
)
def test_spec_permitted_retries_are_declared_retryable(error_type: ErrorType):
    """Spec §8.3: 429, 502/503/504 and transient transport failures may be retried."""

    assert error_type in RETRYABLE


def test_a_partially_delivered_stream_is_never_replayed():
    # Replaying would duplicate text, tool calls and file changes that already reached the caller.
    assert not is_retryable(ErrorType.CONNECTION_LOST)
    assert not is_retryable(ErrorType.STREAM_INTERRUPTED)


def test_local_service_failures_are_not_retried():
    for error_type in (
        ErrorType.LOCAL_SERVER_UNAVAILABLE,
        ErrorType.LOCAL_MODEL_NOT_LOADED,
        ErrorType.LOCAL_MODEL_OUT_OF_MEMORY,
    ):
        assert not is_retryable(error_type)


def test_an_unclassified_error_defaults_to_not_retrying():
    assert not is_retryable(ErrorType.UNKNOWN)


# --------------------------------------------------------------------------- http mapping


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ErrorType.AUTHENTICATION_FAILED),
        (402, ErrorType.INSUFFICIENT_BALANCE),
        (403, ErrorType.PERMISSION_DENIED),
        (404, ErrorType.MODEL_NOT_FOUND),
        (429, ErrorType.PROVIDER_RATE_LIMITED),
        (500, ErrorType.PROVIDER_OVERLOADED),
        (502, ErrorType.PROVIDER_OVERLOADED),
        (503, ErrorType.PROVIDER_OVERLOADED),
        (504, ErrorType.PROVIDER_OVERLOADED),
        (400, ErrorType.INVALID_REQUEST),
        (418, ErrorType.INVALID_REQUEST),
        (301, ErrorType.UNKNOWN),
    ],
)
def test_http_status_classification(status: int, expected: ErrorType):
    assert classify_http_status(status) is expected


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404])
def test_client_error_statuses_map_to_non_retryable_types(status: int):
    """Spec §8.3 forbids retrying any of these."""

    assert not is_retryable(classify_http_status(status))


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_transient_server_statuses_map_to_retryable_types(status: int):
    assert is_retryable(classify_http_status(status))


# --------------------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    "text",
    [
        "failed with key sk-abcdef123456",
        "Authorization: Bearer abcdef123456",
        "nvapi-abcdef123456 rejected",
    ],
)
def test_key_shaped_tokens_are_redacted(text: str):
    assert "[redacted]" in redact_secrets(text)


def test_ordinary_diagnostics_survive_redaction():
    message = "model gemini-3-pro-preview is not available in region europe-west4"
    assert redact_secrets(message) == message
