"""Error taxonomy and classification (spec §43, §44).

Provider and CLI adapters convert their native failures into one of these types, so the run
pipeline can decide retry/no-retry uniformly.
"""

from __future__ import annotations

from enum import Enum


class ErrorType(str, Enum):
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    MODEL_NOT_FOUND = "model_not_found"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_OVERLOADED = "provider_overloaded"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    INVALID_REQUEST = "invalid_request"
    CONTEXT_LIMIT = "context_limit"
    #: A provider returned a truncated/incomplete result that is not a usable completion (spec §12).
    INCOMPLETE_RESPONSE = "incomplete_response"
    CONTENT_FILTERED = "content_filtered"
    TOOL_FAILED = "tool_failed"
    COMMAND_FAILED = "command_failed"
    TEST_FAILED = "test_failed"
    CLI_NOT_FOUND = "cli_not_found"
    CLI_VERSION_UNSUPPORTED = "cli_version_unsupported"
    SESSION_NOT_FOUND = "session_not_found"
    WORKSPACE_CONFLICT = "workspace_conflict"
    USER_CANCELLED = "user_cancelled"
    TIMEOUT = "timeout"
    #: The stream dropped after we had already yielded events — not safe to replay (spec §44).
    CONNECTION_LOST = "connection_lost"
    #: The provider accepted the request asynchronously (HTTP 202 + request id). OpenAgent's chat
    #: runtime is synchronous and does not poll, so a 202 is an explicit failure, never an empty
    #: success (spec §15.5, some NVIDIA model types).
    ASYNC_UNSUPPORTED = "async_unsupported"
    UNKNOWN = "unknown"


#: Errors that are safe to retry automatically (spec §44).
RETRYABLE = {
    ErrorType.PROVIDER_RATE_LIMITED,
    ErrorType.PROVIDER_OVERLOADED,
    ErrorType.TIMEOUT,
}

#: Errors that must never be retried (spec §44).
NON_RETRYABLE = {
    ErrorType.AUTHENTICATION_FAILED,
    ErrorType.PERMISSION_DENIED,
    ErrorType.INVALID_REQUEST,
    ErrorType.INSUFFICIENT_BALANCE,
}


class OpenAgentError(Exception):
    """Base error carrying a classified :class:`ErrorType`."""

    def __init__(self, error_type: ErrorType, message: str = "") -> None:
        super().__init__(message or error_type.value)
        self.error_type = error_type
        self.message = message or error_type.value


class MaxStepsExceeded(OpenAgentError):
    def __init__(self, steps: int) -> None:
        super().__init__(ErrorType.UNKNOWN, f"agent exceeded {steps} steps")


def classify_http_status(status: int) -> ErrorType:
    """Map an HTTP status code to an :class:`ErrorType` (spec §43)."""

    if status == 401:
        return ErrorType.AUTHENTICATION_FAILED
    if status == 403:
        return ErrorType.PERMISSION_DENIED
    if status == 404:
        return ErrorType.MODEL_NOT_FOUND
    if status == 429:
        return ErrorType.PROVIDER_RATE_LIMITED
    if status in (500, 502, 503, 504):
        return ErrorType.PROVIDER_OVERLOADED
    if status == 402:
        return ErrorType.INSUFFICIENT_BALANCE
    if 400 <= status < 500:
        return ErrorType.INVALID_REQUEST
    return ErrorType.UNKNOWN


def is_retryable(error_type: ErrorType) -> bool:
    return error_type in RETRYABLE
