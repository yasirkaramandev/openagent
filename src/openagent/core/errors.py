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
    MALFORMED_STREAM = "malformed_stream"
    INVALID_TOOL_CALL = "invalid_tool_call"
    INVALID_TOOL_ARGUMENTS = "invalid_tool_arguments"
    OUTPUT_LIMIT_EXCEEDED = "output_limit_exceeded"
    #: The on-disk database was written by a newer OpenAgent whose domain shape this binary cannot
    #: safely read (spec §6). Distinct from a corrupt row (:data:`DATA_VALIDATION`) — the data is
    #: fine, the *reader* is too old.
    DATABASE_INCOMPATIBLE = "database_incompatible"
    #: A persisted record could not be decoded into its current domain model. The store is otherwise
    #: intact; the single record is quarantined rather than crashing the surface that read it.
    DATA_VALIDATION = "data_validation"
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


class DatabaseReaderCompatibilityError(OpenAgentError):
    """A newer OpenAgent wrote this database; the active (older) binary must not read it (spec §6).

    The failure the user actually hit was a raw Pydantic ``ValidationError`` deep inside
    ``ProviderConnection.model_validate`` — an old binary whose domain model predated a JSON field a
    newer binary had written. The integer schema number was identical in both, so the schema-version
    guard never fired. This typed error is raised **before** any ORM/model load, from metadata alone,
    so the TUI shows a recovery screen, the CLI a short line, and doctor a structured check — never a
    traceback. It carries everything those surfaces need to tell the user exactly what to run.
    """

    def __init__(
        self,
        *,
        database_schema: int | None,
        supported_schema_min: int,
        supported_schema_max: int,
        database_writer_version: str | None,
        minimum_reader_version: str | None,
        binary_version: str,
        binary_path: str,
        repair_commands: list[str],
    ) -> None:
        self.database_schema = database_schema
        self.supported_schema_min = supported_schema_min
        self.supported_schema_max = supported_schema_max
        self.database_writer_version = database_writer_version
        self.minimum_reader_version = minimum_reader_version
        self.binary_version = binary_version
        self.binary_path = binary_path
        self.repair_commands = repair_commands
        wrote = database_writer_version or "a newer OpenAgent"
        required = minimum_reader_version or "a newer version"
        repair = "\n".join(f"  {command}" for command in repair_commands)
        message = (
            f"Database was written by OpenAgent {wrote}.\n"
            f"This binary is older and cannot safely read it.\n\n"
            f"Active binary: {binary_path}\n"
            f"Active version: {binary_version}\n"
            f"Required version: >= {required}\n\n"
            f"Repair:\n{repair}"
        )
        super().__init__(ErrorType.DATABASE_INCOMPATIBLE, message)


class DataValidationError(OpenAgentError):
    """A single persisted record could not be decoded into its current domain model (spec §7.3).

    Raised in place of a raw ``ValidationError`` so a surface listing records degrades to a typed,
    redacted message ("record X could not be decoded; no data was changed") instead of a traceback.
    Never carries the offending payload, which may hold a credential reference, header or URL.
    """

    def __init__(self, *, table: str, record_id: str, error_count: int) -> None:
        self.table = table
        self.record_id = record_id
        self.error_count = error_count
        super().__init__(
            ErrorType.DATA_VALIDATION,
            f"record {record_id!r} in {table} could not be decoded "
            f"({error_count} schema error(s)); no data was changed. Run: openagent doctor",
        )


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
