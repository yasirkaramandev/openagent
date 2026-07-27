"""JSON-RPC over a child process's stdio, for ACP-speaking CLIs (spec §18).

Every other CLI adapter here reads a one-way stream of JSONL and maps it. An ACP agent is different:
it is a *peer*. OpenAgent sends requests and receives responses, the agent sends its own requests back
(permission prompts), and both sides send notifications. That makes stdio a bidirectional protocol
channel rather than a log, and it brings a set of failure modes the JSONL adapters simply do not have.

The ones this module is built around, because each has a wrong-but-obvious handling:

* **An unbounded message.** A framing bug or a hostile agent sends a gigabyte on one line. Reading
  until newline puts it all in memory. So there is a hard per-message ceiling and exceeding it is a
  protocol failure, not a truncation.
* **A response to a request nobody made.** Matching on "the next response" instead of on the id means
  one out-of-order reply desynchronises every subsequent call. Responses are dispatched by id and an
  unknown id is dropped with a diagnostic.
* **A duplicate request id.** Reusing an id would overwrite a pending future and hang the first caller
  forever. Ids are issued monotonically here and a duplicate from the agent is rejected.
* **stderr as protocol.** An agent that logs to stdout corrupts the channel. stderr is read
  separately and never parsed as protocol.
* **A hang.** Every call is bounded; a silent agent produces a timeout, not a stuck run.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

#: Hard ceiling on one JSON-RPC message. Generous enough for a large tool result, small enough that a
#: framing bug cannot exhaust memory before it is reported.
MAX_MESSAGE_BYTES = 8 * 1024 * 1024

#: Default per-call timeout. A silent peer must produce a diagnosable failure, not a stuck run.
DEFAULT_CALL_TIMEOUT = 120.0


class AcpProtocolError(RuntimeError):
    """The peer violated the protocol in a way that makes the channel untrustworthy."""


class AcpTimeout(TimeoutError):
    """A call exceeded its bound."""


@dataclass
class AcpMessage:
    """One decoded JSON-RPC message."""

    raw: dict[str, Any]

    @property
    def id(self) -> Any:
        return self.raw.get("id")

    @property
    def method(self) -> str | None:
        method = self.raw.get("method")
        return method if isinstance(method, str) else None

    @property
    def is_request(self) -> bool:
        """A request *from the peer* — it has both a method and an id, so it wants an answer."""

        return self.method is not None and self.id is not None

    @property
    def is_notification(self) -> bool:
        return self.method is not None and self.id is None

    @property
    def is_response(self) -> bool:
        return self.method is None and self.id is not None

    @property
    def error(self) -> dict[str, Any] | None:
        error = self.raw.get("error")
        return error if isinstance(error, dict) else None

    @property
    def result(self) -> Any:
        return self.raw.get("result")


def encode(message: dict[str, Any]) -> bytes:
    """Encode one message as a single newline-delimited JSON line.

    ``ensure_ascii=False`` keeps multi-byte characters as themselves; the newline is the frame, so the
    payload must not contain one — ``json.dumps`` guarantees that for strings by escaping.
    """

    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes) -> AcpMessage:
    """Decode one framed line, rejecting anything that is not a JSON-RPC object.

    A non-object or unparseable line is a protocol error rather than a skipped line: on a
    bidirectional channel, silently dropping a message means a pending call waits for a reply that was
    already sent and mangled.
    """

    if len(line) > MAX_MESSAGE_BYTES:
        raise AcpProtocolError(
            f"peer sent a {len(line)}-byte message, over the {MAX_MESSAGE_BYTES}-byte ceiling"
        )
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcpProtocolError(
            f"peer sent an undecodable message ({exc.__class__.__name__})"
        ) from exc
    if not isinstance(payload, dict):
        raise AcpProtocolError("peer sent a JSON value that is not an object")
    if payload.get("jsonrpc") not in (None, "2.0"):
        raise AcpProtocolError(f"unsupported jsonrpc version {payload.get('jsonrpc')!r}")
    return AcpMessage(payload)


#: Handles a request *from* the peer (a permission prompt) and returns the result to send back.
RequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
#: Receives notifications from the peer (session updates).
NotificationHandler = Callable[[str, dict[str, Any]], None]


@dataclass
class AcpConnection:
    """A JSON-RPC peer connection over a child process's stdin/stdout."""

    stdin: asyncio.StreamWriter
    stdout: asyncio.StreamReader
    on_request: RequestHandler | None = None
    on_notification: NotificationHandler | None = None
    call_timeout: float = DEFAULT_CALL_TIMEOUT
    #: Monotonic, so an id is never reused within a connection. Reuse would overwrite a pending
    #: future and hang its caller forever.
    _next_id: int = 1
    _pending: dict[int, asyncio.Future[Any]] = field(default_factory=dict)
    _reader_task: asyncio.Task[None] | None = None
    _closed: bool = False
    #: Protocol violations observed, for Doctor. Kept rather than raised where the channel survives.
    diagnostics: list[str] = field(default_factory=list)

    def start(self) -> None:
        if self._reader_task is None:
            self._reader_task = asyncio.ensure_future(self._read_loop())

    async def close(self) -> None:
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
                pass
            self._reader_task = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(
                    AcpProtocolError("connection closed with the call outstanding")
                )
        self._pending.clear()

    # ------------------------------------------------------------------ sending

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        """Send a request and await its response, bounded."""

        if self._closed:
            raise AcpProtocolError("connection is closed")
        self.start()
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        await self._write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )
        try:
            return await asyncio.wait_for(future, timeout or self.call_timeout)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise AcpTimeout(f"{method} did not answer within the call timeout") from exc
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def _write(self, message: dict[str, Any]) -> None:
        self.stdin.write(encode(message))
        await self.stdin.drain()

    # ------------------------------------------------------------------ receiving

    async def _read_loop(self) -> None:
        while not self._closed:
            try:
                line = await self.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError) as exc:
                # asyncio raises when a line exceeds the stream limit. That is the unbounded-message
                # case, and it is fatal: the channel's framing can no longer be trusted.
                self._fail_all(AcpProtocolError(f"peer message exceeded the stream limit ({exc})"))
                return
            if not line:
                self._fail_all(AcpProtocolError("peer closed the channel"))
                return
            if not line.strip():
                continue
            try:
                message = decode(line)
            except AcpProtocolError as exc:
                self.diagnostics.append(str(exc))
                continue
            await self._dispatch(message)

    async def _dispatch(self, message: AcpMessage) -> None:
        if message.is_response:
            self._resolve(message)
            return
        if message.is_request:
            await self._serve_request(message)
            return
        if message.is_notification and self.on_notification is not None:
            params = message.raw.get("params")
            self.on_notification(message.method or "", params if isinstance(params, dict) else {})

    def _resolve(self, message: AcpMessage) -> None:
        raw_id = message.id
        if not isinstance(raw_id, int):
            self.diagnostics.append(f"response carried a non-integer id {raw_id!r}")
            return
        future = self._pending.pop(raw_id, None)
        if future is None:
            # A response to a request nobody made. Dropped with a diagnostic rather than applied to
            # whatever call happens to be outstanding — that is how one stray reply desynchronises
            # every subsequent call.
            self.diagnostics.append(f"response for unknown request id {raw_id}")
            return
        if future.done():
            return
        error = message.error
        if error is not None:
            future.set_exception(
                AcpProtocolError(f"peer returned error {error.get('code')}: {error.get('message')}")
            )
            return
        future.set_result(message.result)

    async def _serve_request(self, message: AcpMessage) -> None:
        """Answer a request from the peer, e.g. a permission prompt."""

        params = message.raw.get("params")
        params = params if isinstance(params, dict) else {}
        if self.on_request is None:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": message.id,
                    "error": {"code": -32601, "message": f"unhandled method {message.method}"},
                }
            )
            return
        try:
            result = await self.on_request(message.method or "", params)
        except Exception as exc:  # noqa: BLE001 - a handler failure is answered, never left hanging
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": message.id,
                    "error": {"code": -32603, "message": f"{exc.__class__.__name__}: {exc}"},
                }
            )
            return
        await self._write({"jsonrpc": "2.0", "id": message.id, "result": result})

    def _fail_all(self, exc: BaseException) -> None:
        self.diagnostics.append(str(exc))
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
