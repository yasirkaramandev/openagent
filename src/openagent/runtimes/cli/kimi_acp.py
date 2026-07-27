"""Kimi ACP adapter (spec §18).

Kimi's CLI speaks the Agent Client Protocol: JSON-RPC over stdio, with the agent as a *peer* rather
than a log producer. That changes the shape of the adapter and it changes the security surface, both
of which are handled in :mod:`.acp`; this module is the lifecycle and the mapping.

The lifecycle is explicit because each step can fail in a way the next step would otherwise paper over:
spawn, ``initialize`` (which is also the capability handshake — what the agent says it can do, not what
the documentation says), an auth check, session create or load, prompt, then events until a terminal
stop reason, then shutdown.

**Permission prompts arrive as requests to us.** The agent asks whether a tool call may proceed, and
answering "yes" by default would make every permission profile decorative. The handler here answers
from the profile the run was started with, and defaults to refusing anything the profile does not
positively allow.

Marked experimental: mapping is fixture-validated, and no live Kimi ACP session has been observed by
this build. Experimental here means "the contract may be wrong", not "it does not run".
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ...core.errors import ErrorType
from ...core.events import EventType, NormalizedEvent
from ...core.models import CliInstallation
from ...core.permissions import READ_ONLY, SAFE_EDIT
from ...security.process import TerminationOutcome, TerminationResult, minimal_environment
from .acp import AcpConnection, AcpProtocolError, AcpTimeout
from .base import AuthStatus, CliCapabilities, CliModelDiscoveryContext, CliRunRequest
from .installations import inspect_installation
from .locator import CliLocation
from .locator import locate_candidates as locate_cli_candidates
from .model_discovery import CliModelDiscoveryResult

SOURCE = "kimi-acp"

#: The ACP version this adapter implements. Sent in ``initialize``; a peer that answers with a
#: different major version is refused rather than spoken to hopefully.
PROTOCOL_VERSION = 1

#: Credential variables Kimi's CLI documents. Names only.
AUTH_ENVIRONMENT_VARIABLES = ("MOONSHOT_API_KEY", "KIMI_API_KEY")

#: Argument that puts the CLI into ACP mode. Verified against ``--help`` before use, like every other
#: flag in this package.
ACP_FLAG = "--acp"


@dataclass(frozen=True)
class AcpPermissionPolicy:
    """What the agent is allowed to do without asking a human (spec §18.3).

    Deliberately an allowlist. The alternative — a denylist — means any tool the agent adds after this
    was written is permitted by default, which is the wrong direction for a system that runs commands.
    """

    allow_read: bool = True
    allow_edit: bool = False
    allow_execute: bool = False
    note: str = ""

    def decide(self, tool_kind: str) -> bool:
        kind = tool_kind.lower()
        if kind in {"read", "fetch", "search", "list", "glob", "grep"}:
            return self.allow_read
        if kind in {"edit", "write", "create", "delete", "move", "patch"}:
            return self.allow_edit
        if kind in {"execute", "shell", "bash", "command", "run", "terminal"}:
            return self.allow_execute
        # An unrecognised kind is refused. A tool nobody classified is a tool nobody reviewed.
        return False


_POLICIES: dict[str, AcpPermissionPolicy] = {
    READ_ONLY: AcpPermissionPolicy(note="reads allowed; edits and commands refused"),
    SAFE_EDIT: AcpPermissionPolicy(
        allow_edit=True, note="reads and edits allowed; commands refused"
    ),
    "full": AcpPermissionPolicy(
        allow_edit=True, allow_execute=True, note="reads, edits and commands allowed"
    ),
    "plan": AcpPermissionPolicy(note="reads only; the agent is asked to plan without acting"),
}


def permission_policy(profile_name: str) -> AcpPermissionPolicy:
    """An unknown profile gets read-only — the direction a default should fail in."""

    return _POLICIES.get(profile_name, _POLICIES[READ_ONLY])


# ------------------------------------------------------------------------------- mapping


def map_session_update(params: dict[str, Any], run_id: str) -> list[NormalizedEvent]:
    """Map one ACP ``session/update`` notification onto normalized events (spec §18.2).

    Pure and separately tested. An unrecognised update kind yields nothing: on a bidirectional
    protocol, guessing at an unknown notification is how a run ends early or reports something that
    did not happen.
    """

    update = _dict(params.get("update"))
    kind = str(update.get("sessionUpdate") or update.get("kind") or "")

    if kind in {"agent_message_chunk", "agent_message_delta"}:
        text = _text_of(update.get("content"))
        if text:
            return [
                NormalizedEvent(
                    type=EventType.MESSAGE_DELTA,
                    run_id=run_id,
                    source=SOURCE,
                    data={"text": text},
                )
            ]
        return []

    if kind == "agent_message":
        text = _text_of(update.get("content"))
        if text:
            return [
                NormalizedEvent(
                    type=EventType.MESSAGE_COMPLETED,
                    run_id=run_id,
                    source=SOURCE,
                    data={"text": text},
                )
            ]
        return []

    if kind == "agent_thought_chunk":
        # Reasoning is accumulated by the model, not surfaced. No adapter here emits raw reasoning,
        # and making this one the exception would surface thoughts the moment a renderer reads the
        # field.
        return []

    if kind == "tool_call":
        return [
            NormalizedEvent(
                type=EventType.TOOL_STARTED,
                run_id=run_id,
                source=SOURCE,
                data={
                    "tool": str(update.get("title") or update.get("kind") or ""),
                    "tool_use_id": str(update.get("toolCallId") or ""),
                },
            )
        ]

    if kind == "tool_call_update":
        status = str(update.get("status") or "")
        if status in {"completed", "failed", "cancelled"}:
            return [
                NormalizedEvent(
                    type=EventType.TOOL_COMPLETED,
                    run_id=run_id,
                    source=SOURCE,
                    data={
                        "tool_use_id": str(update.get("toolCallId") or ""),
                        "is_error": status != "completed",
                        "status": status,
                    },
                )
            ]
        return []

    if kind == "plan":
        entries = update.get("entries")
        if isinstance(entries, list) and entries:
            return [
                NormalizedEvent(
                    type=EventType.PLAN_UPDATED,
                    run_id=run_id,
                    source=SOURCE,
                    data={
                        "steps": [_plan_entry(item) for item in entries if isinstance(item, dict)]
                    },
                )
            ]
        return []

    return []


def _plan_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": str(entry.get("content") or ""),
        "status": str(entry.get("status") or ""),
    }


def terminal_event(stop_reason: str, run_id: str) -> NormalizedEvent:
    """Map ACP's ``stopReason`` to exactly one terminal event.

    ``refusal`` and ``max_tokens`` are failures rather than completions: the run did not do what was
    asked, and reporting them as completed would make a truncated or declined run look successful.
    """

    reason = (stop_reason or "").lower()
    if reason in {"end_turn", "completed"}:
        return NormalizedEvent(type=EventType.RUN_COMPLETED, run_id=run_id, source=SOURCE, data={})
    if reason == "cancelled":
        return NormalizedEvent(type=EventType.RUN_CANCELLED, run_id=run_id, source=SOURCE, data={})
    error_type = {
        "refusal": ErrorType.CONTENT_FILTERED,
        "max_tokens": ErrorType.OUTPUT_LIMIT_EXCEEDED,
        "max_turn_requests": ErrorType.OUTPUT_LIMIT_EXCEEDED,
    }.get(reason, ErrorType.COMMAND_FAILED)
    return NormalizedEvent(
        type=EventType.RUN_FAILED,
        run_id=run_id,
        source=SOURCE,
        data={"error_type": error_type.value, "message": f"agent stopped: {stop_reason}"},
    )


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(content, list):
        return "".join(_text_of(item) for item in content)
    return ""


# ------------------------------------------------------------------------------- adapter


@dataclass
class AcpHandshake:
    """What the peer said it can do, as opposed to what the documentation says (spec §18.2)."""

    protocol_version: int | None = None
    #: Whether the agent advertises loading a previous session. The only basis for claiming resume.
    load_session: bool = False
    #: Auth methods the agent offers, by id.
    auth_methods: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def compatible(self) -> bool:
        return self.protocol_version == PROTOCOL_VERSION

    @property
    def requires_auth(self) -> bool:
        return bool(self.auth_methods)


def parse_handshake(result: object) -> AcpHandshake:
    """Read an ``initialize`` result without assuming any field is present."""

    if not isinstance(result, dict):
        return AcpHandshake()
    capabilities = result.get("agentCapabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    methods = result.get("authMethods")
    ids: list[str] = []
    if isinstance(methods, list):
        for method in methods:
            if isinstance(method, dict) and isinstance(method.get("id"), str):
                ids.append(method["id"])
            elif isinstance(method, str):
                ids.append(method)
    version = result.get("protocolVersion")
    return AcpHandshake(
        protocol_version=version if isinstance(version, int) else None,
        load_session=bool(capabilities.get("loadSession")),
        auth_methods=tuple(ids),
        raw=result,
    )


class KimiAcpAdapter:
    """Kimi's ACP agent, driven over stdio (spec §18)."""

    cli_type = "kimi"
    model_discovery_method = "kimi-acp-handshake"

    def __init__(self, executable: str | None = None) -> None:
        self._explicit_executable = executable
        self.location: CliLocation = locate_cli_candidates("kimi", explicit_path=executable)
        self.executable = executable or self.location.active_executable
        self._connections: dict[str, AcpConnection] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._handshake: AcpHandshake | None = None
        self._sessions: dict[str, str] = {}
        self.last_model_discovery: CliModelDiscoveryResult | None = None

    # ------------------------------------------------------------------ discovery

    async def detect(self) -> CliInstallation | None:
        self.location = await asyncio.to_thread(self.locate_candidates)
        install = inspect_installation(
            "kimi", self.location, adapter="kimi-acp-jsonrpc", experimental=True
        )
        if install is not None:
            self.executable = install.executable
        return install

    def locate_candidates(self) -> CliLocation:
        self.location = locate_cli_candidates("kimi", explicit_path=self._explicit_executable)
        return self.location

    async def inspect_installation(self) -> CliInstallation | None:
        return await self.detect()

    async def handshake(self, *, refresh: bool = False) -> AcpHandshake:
        """Spawn, ``initialize``, and shut down — so capabilities come from the peer itself.

        Deliberately a full round trip rather than a version guess: ACP capability advertisement is
        the *only* statement about this build that is actually about this build.
        """

        if self._handshake is not None and not refresh:
            return self._handshake
        if not self.executable:
            self._handshake = AcpHandshake()
            return self._handshake
        try:
            self._handshake = await self._probe_handshake()
        except (OSError, AcpProtocolError, AcpTimeout):
            # A failed handshake grants nothing. An adapter that fell back to "assume version 1 and
            # loadSession" would offer a resume that silently starts a new conversation.
            self._handshake = AcpHandshake()
        return self._handshake

    async def _probe_handshake(self) -> AcpHandshake:
        process = await asyncio.create_subprocess_exec(
            self.executable or "kimi",
            ACP_FLAG,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # Never merged into stdout: an agent that logs would corrupt the protocol channel.
            stderr=asyncio.subprocess.PIPE,
            env=minimal_environment(),
        )
        assert process.stdin is not None and process.stdout is not None
        connection = AcpConnection(stdin=process.stdin, stdout=process.stdout, call_timeout=20.0)
        connection.start()
        try:
            result = await connection.call(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
                },
            )
            return parse_handshake(result)
        finally:
            await connection.close()
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except (TimeoutError, asyncio.TimeoutError):
                process.kill()
                await process.wait()

    async def capabilities(self) -> CliCapabilities:
        handshake = await self.handshake()
        return CliCapabilities(
            structured_events=handshake.compatible,
            # Only the agent's own loadSession advertisement may make this True (spec §18.2).
            resumable=handshake.load_session,
            edits_files=True,
            runs_commands=True,
            experimental=True,
        )

    @property
    def live_structured_events(self) -> bool:
        """ACP streams ``agent_message_chunk`` notifications, so deltas are genuinely live."""

        return bool(self._handshake and self._handshake.compatible)

    async def inspect_auth(self) -> AuthStatus:
        present = [name for name in AUTH_ENVIRONMENT_VARIABLES if os.environ.get(name)]
        if present:
            return AuthStatus(
                authenticated=True,
                detail="Moonshot API key present in the environment",
                environment_names=present,
                source="environment",
            )
        handshake = await self.handshake()
        if handshake.requires_auth:
            return AuthStatus(
                authenticated=False,
                detail=(
                    "the agent advertises authentication methods "
                    f"({', '.join(handshake.auth_methods)}) and no credential variable is set"
                ),
                blocking=False,
                source="none",
            )
        return AuthStatus(
            authenticated=False,
            detail="no credential variable set; an interactive login cannot be detected here",
            blocking=False,
            source="none",
        )

    async def list_models(self, context: CliModelDiscoveryContext | None = None) -> list[str]:
        """ACP does not carry a model list, and none is invented.

        The protocol's ``initialize`` advertises *capabilities*, not models. The wizard keeps the
        manual-id path open, which is the honest answer rather than a fabricated list (spec §18).
        """

        self.last_model_discovery = CliModelDiscoveryResult(
            cli_type=self.cli_type,
            available=False,
            method=self.model_discovery_method,
            error=(
                "the Agent Client Protocol does not expose a model list; type a model id, or leave "
                "it blank to use the agent's own default"
            ),
        )
        return []

    # ------------------------------------------------------------------ runs

    def start_run(self, request: CliRunRequest) -> AsyncIterator[NormalizedEvent]:
        return self._drive(request, load_session=None)

    def resume_run(
        self, session_id: str, prompt: str, request: CliRunRequest
    ) -> AsyncIterator[NormalizedEvent]:
        return self._drive(request, load_session=session_id, prompt=prompt)

    async def _drive(
        self,
        request: CliRunRequest,
        *,
        load_session: str | None,
        prompt: str | None = None,
    ) -> AsyncIterator[NormalizedEvent]:
        if not self.executable:
            yield NormalizedEvent(
                run_id=request.run_id,
                type=EventType.RUN_FAILED,
                source=SOURCE,
                data={
                    "error_type": ErrorType.CLI_NOT_FOUND.value,
                    "message": "kimi is not installed",
                },
            )
            return

        handshake = await self.handshake()
        if not handshake.compatible:
            yield NormalizedEvent(
                run_id=request.run_id,
                type=EventType.RUN_FAILED,
                source=SOURCE,
                data={
                    "error_type": ErrorType.CLI_VERSION_UNSUPPORTED.value,
                    "message": (
                        f"the agent answered protocol version {handshake.protocol_version!r}; "
                        f"this build implements {PROTOCOL_VERSION}"
                    ),
                },
            )
            return
        if load_session and not handshake.load_session:
            yield NormalizedEvent(
                run_id=request.run_id,
                type=EventType.RUN_FAILED,
                source=SOURCE,
                data={
                    "error_type": ErrorType.SESSION_NOT_FOUND.value,
                    "message": (
                        "the agent does not advertise loadSession; a new session would not continue "
                        "the recorded one"
                    ),
                },
            )
            return

        policy = permission_policy(request.permission_profile)
        events: asyncio.Queue[NormalizedEvent | None] = asyncio.Queue()

        def on_notification(method: str, params: dict[str, Any]) -> None:
            if method != "session/update":
                return
            for event in map_session_update(params, request.run_id):
                events.put_nowait(event)

        async def on_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            """Answer the agent's own requests — chiefly permission prompts.

            Decided by the run's permission profile. Answering "allow" by default would make every
            profile decorative, which is the failure this whole path exists to prevent.
            """

            if method == "session/request_permission":
                return _permission_response(params, policy)
            if method in {"fs/read_text_file", "fs/write_text_file"}:
                # Refused: OpenAgent's own workspace-scoped tools mediate file access, and a
                # protocol-level file API would bypass the workspace boundary entirely.
                raise AcpProtocolError(f"{method} is not offered by this client")
            raise AcpProtocolError(f"unhandled method {method}")

        process = await asyncio.create_subprocess_exec(
            self.executable,
            ACP_FLAG,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(request.workspace),
            env=minimal_environment(request.credential_env),
        )
        assert process.stdin is not None and process.stdout is not None
        connection = AcpConnection(
            stdin=process.stdin,
            stdout=process.stdout,
            on_request=on_request,
            on_notification=on_notification,
        )
        connection.start()
        self._connections[request.run_id] = connection
        self._processes[request.run_id] = process

        yield NormalizedEvent(
            run_id=request.run_id,
            type=EventType.PROCESS_STARTED,
            source=SOURCE,
            data={"pid": process.pid},
        )

        terminal: NormalizedEvent | None = None
        try:
            await connection.call(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
                },
            )
            if load_session:
                await connection.call(
                    "session/load", {"sessionId": load_session, "cwd": str(request.workspace)}
                )
                session_id = load_session
            else:
                created = await connection.call(
                    "session/new", {"cwd": str(request.workspace), "mcpServers": []}
                )
                session_id = _session_id_of(created)
            if session_id:
                self._sessions[request.run_id] = session_id
                yield NormalizedEvent(
                    run_id=request.run_id,
                    type=EventType.SESSION_RESUMED if load_session else EventType.SESSION_CREATED,
                    source=SOURCE,
                    data={"session_id": session_id},
                )

            prompt_task = asyncio.ensure_future(
                connection.call(
                    "session/prompt",
                    {
                        "sessionId": session_id,
                        "prompt": [{"type": "text", "text": prompt or request.prompt}],
                    },
                    timeout=None,
                )
            )
            # Drain notifications while the prompt is outstanding, so deltas surface live rather than
            # arriving in a batch after the turn finishes.
            while not prompt_task.done() or not events.empty():
                try:
                    event = await asyncio.wait_for(events.get(), timeout=0.1)
                except (TimeoutError, asyncio.TimeoutError):
                    continue
                if event is not None:
                    yield event
            result = await prompt_task
            stop_reason = _stop_reason_of(result)
            terminal = terminal_event(stop_reason, request.run_id)
        except asyncio.CancelledError:
            terminal = NormalizedEvent(
                type=EventType.RUN_CANCELLED, run_id=request.run_id, source=SOURCE, data={}
            )
            raise
        except (AcpProtocolError, AcpTimeout) as exc:
            terminal = NormalizedEvent(
                type=EventType.RUN_FAILED,
                run_id=request.run_id,
                source=SOURCE,
                data={
                    "error_type": (
                        ErrorType.TIMEOUT.value
                        if isinstance(exc, AcpTimeout)
                        else ErrorType.PROTOCOL_MISMATCH.value
                    ),
                    "message": str(exc),
                },
            )
        finally:
            await self._shutdown(request.run_id, connection, process)
            if terminal is not None:
                # Exactly one terminal event per run, always the last thing yielded.
                yield terminal

    async def _shutdown(
        self,
        run_id: str,
        connection: AcpConnection,
        process: asyncio.subprocess.Process,
    ) -> None:
        self._connections.pop(run_id, None)
        self._processes.pop(run_id, None)
        await connection.close()
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except (TimeoutError, asyncio.TimeoutError):
                process.kill()
                await process.wait()

    async def cancel(self, run_id: str) -> TerminationResult:
        """Cancel the agent's turn, then the process if it does not stop.

        A protocol-level cancel first, because it lets the agent finish writing whatever it was
        mid-way through; termination is the fallback, not the first move.
        """

        connection = self._connections.get(run_id)
        process = self._processes.get(run_id)
        if connection is None and process is None:
            return TerminationResult(TerminationOutcome.ALREADY_GONE)
        if connection is not None:
            session_id = self._sessions.get(run_id)
            try:
                await connection.notify(
                    "session/cancel", {"sessionId": session_id} if session_id else {}
                )
            except (AcpProtocolError, OSError, ConnectionError):
                pass
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
                return TerminationResult(TerminationOutcome.TERMINATED)
            except (TimeoutError, asyncio.TimeoutError):
                # SIGTERM was ignored. A forced kill that succeeds still terminated the process, so
                # the outcome is TERMINATED; TerminationOutcome has no separate "killed" member and
                # inventing one would diverge from what every other adapter reports.
                process.kill()
                await process.wait()
                return TerminationResult(TerminationOutcome.TERMINATED)
        return TerminationResult(TerminationOutcome.ALREADY_GONE)

    def session_id_for(self, run_id: str) -> str | None:
        return self._sessions.get(run_id)


def _permission_response(params: dict[str, Any], policy: AcpPermissionPolicy) -> dict[str, Any]:
    """Answer a permission request from the profile, defaulting to refusal.

    The options the agent offers are read from the request rather than assumed: an agent may name its
    allow/reject option ids differently, and inventing an id produces a response it cannot interpret.
    """

    tool_call = _dict(params.get("toolCall"))
    kind = str(tool_call.get("kind") or tool_call.get("title") or "")
    allowed = policy.decide(kind)

    options = params.get("options")
    option_id: str | None = None
    if isinstance(options, list):
        wanted = "allow_once" if allowed else "reject_once"
        for option in options:
            if not isinstance(option, dict):
                continue
            kind_field = str(option.get("kind") or "")
            if kind_field == wanted and isinstance(option.get("optionId"), str):
                option_id = option["optionId"]
                break
        if option_id is None:
            for option in options:
                if isinstance(option, dict) and isinstance(option.get("optionId"), str):
                    kind_field = str(option.get("kind") or "")
                    if allowed and kind_field.startswith("allow"):
                        option_id = option["optionId"]
                        break
                    if not allowed and kind_field.startswith("reject"):
                        option_id = option["optionId"]
                        break
    if option_id is None:
        # No recognisable option: cancel rather than guess. Guessing an id when the decision is
        # "deny" risks selecting an allow option.
        return {"outcome": {"outcome": "cancelled"}}
    return {"outcome": {"outcome": "selected", "optionId": option_id}}


def _session_id_of(result: object) -> str:
    if isinstance(result, dict):
        for key in ("sessionId", "session_id", "id"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _stop_reason_of(result: object) -> str:
    if isinstance(result, dict):
        for key in ("stopReason", "stop_reason"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
    return "end_turn"


def _dict(value: object) -> dict[str, Any]:
    """``value`` if it is a mapping, else an empty one.

    A named helper rather than an inline ``x if isinstance(x, dict) else {}``: the inline form looks up
    the key twice and, because the isinstance check applies to a *different* call expression, narrows
    nothing — so every downstream ``.get`` is untyped. One helper fixes both.
    """

    return value if isinstance(value, dict) else {}
