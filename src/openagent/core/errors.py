"""Error taxonomy and classification (spec §43, §44).

Provider and CLI adapters convert their native failures into one of these types, so the run
pipeline can decide retry/no-retry uniformly.
"""

from __future__ import annotations

import re
from enum import Enum

#: Key-shaped tokens redacted before an error message reaches a terminal, log or test (spec §17.4).
#: Conservative on purpose: it targets the well-known credential prefixes rather than trying to
#: recognise every possible secret, so it never mangles ordinary diagnostic text.
_SECRET_TOKEN_RE = re.compile(
    r"(sk-[A-Za-z0-9._\-]{6,}|nvapi-[A-Za-z0-9._\-]{6,}|"
    r"Bearer\s+[A-Za-z0-9._\-]{6,}|xox[baprs]-[A-Za-z0-9-]{6,})"
)


def redact_secrets(text: str) -> str:
    """Replace key-shaped tokens with ``[redacted]``. Used at every boundary that renders an error."""

    return _SECRET_TOKEN_RE.sub("[redacted]", text)


class ErrorType(str, Enum):
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    MODEL_NOT_FOUND = "model_not_found"
    #: The provider still routes the model but has announced its removal. Distinct from
    #: MODEL_NOT_FOUND because the request may still succeed today — the user needs to migrate, not
    #: to fix a typo (spec §8.2; e.g. DeepSeek's retired `deepseek-chat`/`deepseek-reasoner`
    #: aliases).
    MODEL_DEPRECATED = "model_deprecated"
    #: The model exists and the credential is valid, but it cannot do what the request needs — a
    #: tool call sent to a model with no tool support, say. Retrying cannot help; choosing another
    #: model can.
    MODEL_CAPABILITY_MISSING = "model_capability_missing"
    #: The endpoint rejected a *parameter*, not the content. Separated from INVALID_REQUEST so the
    #: compatibility profile that sent it can be corrected instead of the user's prompt.
    UNSUPPORTED_PARAMETER = "unsupported_parameter"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_OVERLOADED = "provider_overloaded"
    #: The credential is valid but belongs to a different region/workspace than the endpoint being
    #: called. Reads as an auth failure on the wire and is not one — the fix is the endpoint, not
    #: the key (spec §16, §18.3).
    PROVIDER_REGION_MISMATCH = "provider_region_mismatch"
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
    #: The transport stayed up and the provider stopped sending without a terminal event. Distinct
    #: from CONNECTION_LOST, which is the socket dropping: here the turn is incomplete but nothing
    #: failed at the network layer, so the remedy is to treat the turn as unfinished rather than to
    #: diagnose connectivity (spec §8.2).
    STREAM_INTERRUPTED = "stream_interrupted"
    INVALID_TOOL_CALL = "invalid_tool_call"
    INVALID_TOOL_ARGUMENTS = "invalid_tool_arguments"
    #: A continuation needs the previous assistant turn (its tool calls, and for some providers the
    #: reasoning that accompanied them) and it is not available. Sending the request anyway earns a
    #: provider 400 at best and a silently degraded turn at worst (spec §10, §15.3).
    TOOL_HISTORY_INCOMPLETE = "tool_history_incomplete"
    #: A stored continuation envelope cannot be replayed: wrong provider or protocol, a failed
    #: integrity hash, or a schema this build does not understand (spec §8.4).
    CONTINUATION_INVALID = "continuation_invalid"
    #: The provider was holding the conversation state and no longer is. Server-side resume is gone;
    #: only a new session or a client-side replay remains (spec §26).
    REMOTE_SESSION_EXPIRED = "remote_session_expired"
    #: A response was requested in a structured shape and did not arrive in it.
    STRUCTURED_OUTPUT_FAILED = "structured_output_failed"
    #: The endpoint answered in a protocol other than the one the adapter speaks — an
    #: OpenAI-compatible URL that turns out to serve something else, or a version skew.
    PROTOCOL_MISMATCH = "protocol_mismatch"
    OUTPUT_LIMIT_EXCEEDED = "output_limit_exceeded"
    #: The request never reached the provider: connection refused, DNS failure, no route. Distinct
    #: from TIMEOUT (the endpoint accepted the connection and did not answer in time) and from
    #: CONNECTION_LOST (a stream that had already delivered events). The distinction is what tells a
    #: local-provider user to *start their server* rather than to wait (spec §8.2, §12.4).
    NETWORK_UNAVAILABLE = "network_unavailable"
    #: TLS negotiation failed — an untrusted or expired certificate, a hostname mismatch, a protocol
    #: version refusal. Never retried and never downgraded: a remote endpoint that cannot prove its
    #: identity is not one to send a credential to (spec §23.3, §27).
    TLS_ERROR = "tls_error"

    # --- local provider services (spec §12, §13) -------------------------------------------
    #: The local server (Ollama, LM Studio) is not reachable. Not retried: a daemon that is down
    #: stays down until someone starts it, and backing off just delays the message that says so.
    LOCAL_SERVER_UNAVAILABLE = "local_server_unavailable"
    #: The server is up and the model is not loaded into memory. Recoverable by an explicit,
    #: user-approved load — never by OpenAgent loading it unasked.
    LOCAL_MODEL_NOT_LOADED = "local_model_not_loaded"
    #: The model could not be loaded or kept resident for want of memory. Retrying the same request
    #: on the same machine reproduces it.
    LOCAL_MODEL_OUT_OF_MEMORY = "local_model_out_of_memory"

    # --- model catalog (spec §7, §25.3) ----------------------------------------------------
    #: The catalog could not be read at all. Emphatically *not* the same as an empty catalog: the
    #: wizard must offer a retry and a manual model ID, not report that the provider has no models.
    CATALOG_UNAVAILABLE = "catalog_unavailable"
    #: Some catalog entries parsed and others did not. The usable ones are still offered, labelled
    #: as incomplete, rather than the whole list being discarded or the gap being hidden.
    CATALOG_PARTIAL = "catalog_partial"
    #: The on-disk database was written by a newer OpenAgent whose domain shape this binary cannot
    #: safely read (spec §6). Distinct from a corrupt row (:data:`DATA_VALIDATION`) — the data is
    #: fine, the *reader* is too old.
    DATABASE_INCOMPATIBLE = "database_incompatible"
    #: A persisted record could not be decoded into its current domain model. The store is otherwise
    #: intact; the single record is quarantined rather than crashing the surface that read it.
    DATA_VALIDATION = "data_validation"
    UNKNOWN = "unknown"


#: Errors that are safe to retry automatically (spec §8.3, §44).
#:
#: Deliberately small. An error belongs here only when the *same request* has a real chance of
#: succeeding unchanged after a wait — a rate limit, an overloaded upstream, a timeout. Everything
#: else either needs a different request or a human, and retrying it converts one clear failure into
#: several slow identical ones while spending the user's quota.
RETRYABLE = {
    ErrorType.PROVIDER_RATE_LIMITED,
    ErrorType.PROVIDER_OVERLOADED,
    ErrorType.TIMEOUT,
    # A connection that never opened may be a transient DNS or routing failure, and no request was
    # delivered, so replaying it cannot duplicate anything (spec §8.3 permits connection reset and
    # temporary DNS). A genuinely down server exhausts the small retry budget and then reports
    # honestly — the cost is one bounded backoff, not a wrong diagnosis.
    ErrorType.NETWORK_UNAVAILABLE,
}

#: Errors that must never be retried (spec §8.3, §44).
#:
#: This is an assertion, not a filter: :func:`is_retryable` already answers by membership in
#: :data:`RETRYABLE`, so nothing here changes behaviour on its own. It exists so that a future
#: change which adds one of these to ``RETRYABLE`` fails a test instead of quietly billing the user
#: four times for the same rejected request. The two sets are asserted disjoint.
NON_RETRYABLE = {
    ErrorType.AUTHENTICATION_FAILED,
    ErrorType.PERMISSION_DENIED,
    ErrorType.PROVIDER_REGION_MISMATCH,
    ErrorType.INVALID_REQUEST,
    ErrorType.UNSUPPORTED_PARAMETER,
    ErrorType.INSUFFICIENT_BALANCE,
    # A 404 for a model, a retired alias, and a model that cannot do the thing are all settled
    # facts about the request; waiting does not change any of them (spec §8.3).
    ErrorType.MODEL_NOT_FOUND,
    ErrorType.MODEL_DEPRECATED,
    ErrorType.MODEL_CAPABILITY_MISSING,
    ErrorType.CONTEXT_LIMIT,
    ErrorType.OUTPUT_LIMIT_EXCEEDED,
    # Malformed tool traffic is deterministic: the same schema and the same arguments produce the
    # same rejection.
    ErrorType.INVALID_TOOL_CALL,
    ErrorType.INVALID_TOOL_ARGUMENTS,
    ErrorType.TOOL_HISTORY_INCOMPLETE,
    # Resume state is either valid or gone. Neither improves with a backoff.
    ErrorType.CONTINUATION_INVALID,
    ErrorType.REMOTE_SESSION_EXPIRED,
    ErrorType.PROTOCOL_MISMATCH,
    # A local daemon that is down stays down until someone starts it, and a model that did not fit
    # in memory will not fit four seconds later.
    ErrorType.LOCAL_SERVER_UNAVAILABLE,
    ErrorType.LOCAL_MODEL_NOT_LOADED,
    ErrorType.LOCAL_MODEL_OUT_OF_MEMORY,
    # Replaying a stream that already delivered events would duplicate its text, tool calls and file
    # changes. Recovery is a caller-level decision, never an automatic one (spec §44).
    ErrorType.CONNECTION_LOST,
    ErrorType.STREAM_INTERRUPTED,
    ErrorType.USER_CANCELLED,
    # A certificate that does not validate now will not validate on the next attempt, and retrying
    # a TLS failure is how a downgrade gets normalized into "flaky network".
    ErrorType.TLS_ERROR,
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
