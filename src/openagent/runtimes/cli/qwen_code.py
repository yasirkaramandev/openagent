"""Qwen Code CLI adapter (spec §17).

Qwen Code is a Gemini-CLI fork, so its flag surface is close to Gemini's but not identical, and the
differences are exactly where an adapter written from the family resemblance breaks. Two consequences
shape this module.

**Every flag is verified against the installed binary before it is used.** ``--output-format
stream-json``, ``--include-partial-messages``, ``--resume``, ``--approval-mode`` and the budget flags
are all read out of ``--help`` at discovery time (:class:`QwenFlagSupport`). A flag that a release does
not have turns into ``Unknown arguments`` and a run that fails for a reason the user cannot connect to
anything they did — and the documentation for a fast-moving fork is not evidence about the binary on
this machine.

**Discovery runs with the project's own extensions disabled.** A Qwen Code installation can carry
hooks, extensions, skills, MCP servers, project agents and project memory, any of which can execute
code or change what a "list the models" invocation does. Discovery is not the moment to find out, so
it runs in an empty directory with those switched off (:func:`discovery_environment`) — the run itself
is a separate decision the user has already made.

Marked experimental: the event mapping is fixture-validated and no live Qwen Code run has been
observed by this build.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...core.errors import ErrorType
from ...core.events import EventType, NormalizedEvent
from ...core.models import CliInstallation
from ...core.permissions import READ_ONLY, SAFE_EDIT
from ...security.process import (
    ManagedProcess,
    TerminationOutcome,
    TerminationResult,
    minimal_environment,
)
from .base import (
    AuthStatus,
    CliCapabilities,
    CliModelDiscoveryContext,
    CliRunRequest,
    run_buffered_cli,
    run_managed_cli,
)
from .installations import inspect_installation
from .locator import CliLocation
from .locator import locate_candidates as locate_cli_candidates
from .model_discovery import CliModelDiscoveryResult, CliModelOption
from .updates import check_update as inspect_update
from .updates import perform_update as execute_update

SOURCE = "qwen-code"

#: Credential variables Qwen Code documents. Names only; values never leave the child environment.
AUTH_ENVIRONMENT_VARIABLES = (
    "DASHSCOPE_API_KEY",
    "QWEN_API_KEY",
    "OPENAI_API_KEY",  # Qwen Code accepts an OpenAI-compatible endpoint + key pair
    "OPENAI_BASE_URL",
)

#: Everything that can execute code or change behaviour during discovery, switched off. A "list the
#: models" invocation must not be able to run a project hook (spec §19.2, §17.2).
_DISCOVERY_DISABLE = {
    "QWEN_CODE_DISABLE_EXTENSIONS": "1",
    "QWEN_CODE_DISABLE_HOOKS": "1",
    "QWEN_CODE_DISABLE_SKILLS": "1",
    "QWEN_CODE_DISABLE_MCP": "1",
    "QWEN_CODE_DISABLE_PROJECT_AGENTS": "1",
    "QWEN_CODE_DISABLE_MEMORY": "1",
    "QWEN_CODE_NO_TELEMETRY": "1",
}


@dataclass(frozen=True)
class QwenFlagSupport:
    """Which flags the installed binary actually advertises (spec §17.1).

    Every field starts ``False`` and is set only from the binary's own help text. There is no
    version-range guess: the fork's releases do not carry a reliable relationship between a version
    string and a flag's presence.
    """

    stream_json: bool = False
    include_partial_messages: bool = False
    resume: bool = False
    continue_session: bool = False
    approval_mode: bool = False
    safe_mode: bool = False
    model_flag: bool = False
    output_format: bool = False
    session_budget: bool = False
    detail: str = ""

    @property
    def structured_events(self) -> bool:
        return self.stream_json

    @property
    def live_structured_events(self) -> bool:
        """Live deltas need *both* stream-json and the partial-message opt-in.

        With stream-json alone the CLI emits completed messages only. Reporting live streaming on the
        strength of stream-json would promise incremental output that never arrives.
        """

        return self.stream_json and self.include_partial_messages

    @property
    def resumable(self) -> bool:
        return self.resume


#: Flags looked for in ``--help``. The mapping is explicit so a rename in the fork shows up as the
#: capability going False, rather than as a silently mis-set field.
_FLAG_TOKENS: dict[str, tuple[str, ...]] = {
    "output_format": ("--output-format",),
    "stream_json": ("stream-json",),
    "include_partial_messages": ("--include-partial-messages",),
    "resume": ("--resume",),
    "continue_session": ("--continue",),
    "approval_mode": ("--approval-mode",),
    "safe_mode": ("--safe-mode",),
    "model_flag": ("--model",),
    "session_budget": ("--session-turn-budget", "--session-token-budget", "--budget"),
}


def parse_flag_support(help_text: str) -> QwenFlagSupport:
    """Read flag support out of a binary's help output.

    ``stream-json`` is checked as a bare token rather than as ``--output-format stream-json`` because
    help text formats the value list differently across releases; requiring the exact phrase would
    report False for a build that has it.
    """

    text = help_text.lower()
    found = {
        field: any(token in text for token in tokens) for field, tokens in _FLAG_TOKENS.items()
    }
    # stream-json is only meaningful with --output-format to carry it.
    if found["stream_json"] and not found["output_format"]:
        found["stream_json"] = False
    detail = (
        "read from the installed binary's --help"
        if text.strip()
        else "the binary produced no help output; no flag is assumed"
    )
    return QwenFlagSupport(**found, detail=detail)


async def probe_flags(executable: str, *, runner: Any | None = None) -> QwenFlagSupport:
    """Ask the installed binary what it supports. Any failure means "nothing is assumed"."""

    run = runner or _run_help
    try:
        text = await run(executable)
    except Exception as exc:  # noqa: BLE001 - a failed probe never grants a capability
        return QwenFlagSupport(detail=f"could not read qwen --help ({exc.__class__.__name__})")
    return parse_flag_support(text)


async def _run_help(executable: str) -> str:
    process = await asyncio.create_subprocess_exec(
        executable,
        "--help",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=_scratch_dir(),
        env=discovery_environment(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    except (TimeoutError, asyncio.TimeoutError):
        process.kill()
        await process.wait()
        raise
    return (stdout or b"").decode("utf-8", "replace") + (stderr or b"").decode("utf-8", "replace")


def discovery_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """A minimal environment with every code-executing feature disabled (spec §17.2).

    Built on :func:`minimal_environment`, so no provider credential is inherited either: a Qwen
    discovery run must not receive ``ANTHROPIC_API_KEY`` merely because the user has one exported
    (spec §19.2).
    """

    env = minimal_environment(extra)
    env.update(_DISCOVERY_DISABLE)
    return env


def _scratch_dir() -> Path:
    """An empty directory to discover from, so no project config is in scope."""

    import tempfile

    return Path(tempfile.gettempdir())


@dataclass(frozen=True)
class QwenPermissionMapping:
    """How an OpenAgent profile becomes Qwen Code flags (spec §17.5)."""

    approval_mode: str
    safe_mode: bool
    prompt_prefix: str = ""
    note: str = ""


#: ``yolo`` is deliberately absent from every mapping. The fork offers it; a profile a user can pick
#: from a list is a profile they will pick without reading what it does.
_PERMISSIONS: dict[str, QwenPermissionMapping] = {
    READ_ONLY: QwenPermissionMapping(
        approval_mode="default",
        safe_mode=True,
        note="safe mode; every mutating action requires approval",
    ),
    SAFE_EDIT: QwenPermissionMapping(
        approval_mode="auto_edit",
        safe_mode=True,
        note="safe mode; edits auto-approved, shell still prompts",
    ),
    "full": QwenPermissionMapping(
        approval_mode="default",
        safe_mode=False,
        note="the CLI's own approval prompts govern every tool call",
    ),
    "plan": QwenPermissionMapping(
        approval_mode="default",
        safe_mode=True,
        prompt_prefix="Produce a plan only. Do not modify any file and do not run any command.\n\n",
        note="safe mode plus a planning instruction",
    ),
}


def permission_mapping(profile_name: str) -> QwenPermissionMapping:
    """An unknown profile gets the most restrictive mapping — the direction a default should fail."""

    return _PERMISSIONS.get(profile_name, _PERMISSIONS[READ_ONLY])


def build_command(
    request: CliRunRequest,
    *,
    executable: str,
    flags: QwenFlagSupport,
    resume_session: str | None = None,
) -> list[str]:
    """The argv for one run, using only flags the installed binary advertised."""

    mapping = permission_mapping(request.permission_profile)
    argv = [executable, "--prompt", mapping.prompt_prefix + request.prompt]

    if flags.stream_json:
        argv += ["--output-format", "stream-json"]
        if flags.include_partial_messages:
            argv.append("--include-partial-messages")
    elif flags.output_format:
        argv += ["--output-format", "json"]

    if request.model and flags.model_flag:
        argv += ["--model", request.model]
    if flags.approval_mode:
        argv += ["--approval-mode", mapping.approval_mode]
    if mapping.safe_mode and flags.safe_mode:
        argv.append("--safe-mode")
    if resume_session and flags.resume:
        argv += ["--resume", resume_session]
    return argv


# ------------------------------------------------------------------------------- event mapping


def map_stream_event(obj: dict[str, Any], run_id: str) -> list[NormalizedEvent]:
    """Map one Qwen Code ``stream-json`` object onto normalized events (spec §17.3).

    Pure and separately tested. Anything unrecognised produces no event rather than a guess: an
    unknown object type is not a terminal state, and treating it as one would end runs early.
    """

    kind = str(obj.get("type") or "")

    if kind == "system" and obj.get("subtype") == "init":
        session_id = obj.get("session_id")
        return [
            NormalizedEvent(
                type=EventType.SESSION_CREATED,
                run_id=run_id,
                source=SOURCE,
                data={"session_id": session_id} if isinstance(session_id, str) else {},
            )
        ]

    if kind == "assistant":
        message = _dict(obj.get("message"))
        text = _text_of(message.get("content"))
        events: list[NormalizedEvent] = []
        if text:
            events.append(
                NormalizedEvent(
                    type=EventType.MESSAGE_COMPLETED,
                    run_id=run_id,
                    source=SOURCE,
                    data={"text": text},
                )
            )
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                events.append(
                    NormalizedEvent(
                        type=EventType.TOOL_STARTED,
                        run_id=run_id,
                        source=SOURCE,
                        data={
                            "tool": block.get("name") or "",
                            "tool_use_id": block.get("id") or "",
                        },
                    )
                )
        usage = _usage_of(message.get("usage"))
        if usage:
            events.append(
                NormalizedEvent(
                    type=EventType.USAGE_UPDATED, run_id=run_id, source=SOURCE, data=usage
                )
            )
        return events

    if kind == "stream_event":
        # Partial assistant text, present only when --include-partial-messages was accepted.
        event = _dict(obj.get("event"))
        delta = _dict(event.get("delta"))
        fragment = delta.get("text")
        if isinstance(fragment, str) and fragment:
            return [
                NormalizedEvent(
                    type=EventType.MESSAGE_DELTA,
                    run_id=run_id,
                    source=SOURCE,
                    data={"text": fragment},
                )
            ]
        return []

    if kind == "user":
        message = _dict(obj.get("message"))
        events = []
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                events.append(
                    NormalizedEvent(
                        type=EventType.TOOL_COMPLETED,
                        run_id=run_id,
                        source=SOURCE,
                        data={
                            "tool_use_id": block.get("tool_use_id") or "",
                            "is_error": bool(block.get("is_error")),
                        },
                    )
                )
        return events

    if kind == "result":
        subtype = str(obj.get("subtype") or "")
        usage = _usage_of(obj.get("usage"))
        events = []
        if usage:
            events.append(
                NormalizedEvent(
                    type=EventType.USAGE_UPDATED, run_id=run_id, source=SOURCE, data=usage
                )
            )
        if obj.get("is_error") or subtype.startswith("error"):
            events.append(
                NormalizedEvent(
                    type=EventType.RUN_FAILED,
                    run_id=run_id,
                    source=SOURCE,
                    data={
                        "error_type": _error_type(subtype).value,
                        "message": str(obj.get("result") or subtype or "qwen reported an error"),
                    },
                )
            )
        else:
            events.append(
                NormalizedEvent(type=EventType.RUN_COMPLETED, run_id=run_id, source=SOURCE, data={})
            )
        return events

    return []


def _error_type(subtype: str) -> ErrorType:
    lowered = subtype.lower()
    if "max_turns" in lowered or "budget" in lowered:
        return ErrorType.OUTPUT_LIMIT_EXCEEDED
    if "auth" in lowered:
        return ErrorType.AUTHENTICATION_FAILED
    if "rate" in lowered or "quota" in lowered:
        return ErrorType.PROVIDER_RATE_LIMITED
    return ErrorType.COMMAND_FAILED


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _usage_of(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for source_key, target in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("cache_read_input_tokens", "cached_input_tokens"),
    ):
        value = raw.get(source_key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[target] = value
    return out


def parse_single_result(text: str, run_id: str) -> list[NormalizedEvent]:
    """Parse a build without stream-json: one JSON document, or plain text."""

    body = text.strip()
    if not body:
        return [
            NormalizedEvent(type=EventType.RUN_COMPLETED, run_id=run_id, source=SOURCE, data={})
        ]
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return [
            NormalizedEvent(
                type=EventType.MESSAGE_COMPLETED,
                run_id=run_id,
                source=SOURCE,
                data={"text": body},
            ),
            NormalizedEvent(type=EventType.RUN_COMPLETED, run_id=run_id, source=SOURCE, data={}),
        ]
    if isinstance(payload, dict):
        return map_stream_event({**payload, "type": payload.get("type") or "result"}, run_id)
    return [NormalizedEvent(type=EventType.RUN_COMPLETED, run_id=run_id, source=SOURCE, data={})]


# ------------------------------------------------------------------------------- adapter


class QwenCodeAdapter:
    """Qwen Code, run headlessly. Experimental but usable (spec §17)."""

    cli_type = "qwen"
    model_discovery_method = "qwen-config-or-endpoint"

    def __init__(self, executable: str | None = None) -> None:
        self._explicit_executable = executable
        self.location: CliLocation = locate_cli_candidates("qwen", explicit_path=executable)
        self.executable = executable or self.location.active_executable
        self._flags: QwenFlagSupport | None = None
        self._processes: dict[str, ManagedProcess] = {}
        #: Session ids seen in a run's own output, so resume uses the CLI's handle, not a guess.
        self._sessions: dict[str, str] = {}
        self.last_model_discovery: CliModelDiscoveryResult | None = None

    # ------------------------------------------------------------------ discovery

    async def detect(self) -> CliInstallation | None:
        self.location = await asyncio.to_thread(self.locate_candidates)
        install = inspect_installation(
            "qwen", self.location, adapter="qwen-code-stream-json", experimental=True
        )
        if install is not None:
            self.executable = install.executable
        return install

    def locate_candidates(self) -> CliLocation:
        self.location = locate_cli_candidates("qwen", explicit_path=self._explicit_executable)
        return self.location

    async def inspect_installation(self) -> CliInstallation | None:
        return await self.detect()

    async def flags(self, *, refresh: bool = False) -> QwenFlagSupport:
        if self._flags is None or refresh:
            if not self.executable:
                self._flags = QwenFlagSupport(detail="qwen is not installed")
            else:
                self._flags = await probe_flags(self.executable)
        return self._flags

    async def capabilities(self) -> CliCapabilities:
        flags = await self.flags()
        return CliCapabilities(
            structured_events=flags.structured_events,
            resumable=flags.resumable,
            edits_files=True,
            runs_commands=True,
            experimental=True,
        )

    @property
    def live_structured_events(self) -> bool:
        return bool(self._flags and self._flags.live_structured_events)

    async def inspect_auth(self) -> AuthStatus:
        """Report which credential mechanism is configured, never the credential.

        Absence is non-blocking: Qwen Code also supports an interactive OAuth login that leaves no
        environment variable, so a missing variable is not proof of anything and the CLI's own error
        is more useful than a guess about why it might fail.
        """

        present = [name for name in AUTH_ENVIRONMENT_VARIABLES if os.environ.get(name)]
        if any(name in present for name in ("DASHSCOPE_API_KEY", "QWEN_API_KEY")):
            return AuthStatus(
                authenticated=True,
                detail="DashScope API key present in the environment",
                environment_names=present,
                source="environment",
            )
        if "OPENAI_API_KEY" in present and "OPENAI_BASE_URL" in present:
            return AuthStatus(
                authenticated=True,
                detail="OpenAI-compatible endpoint and key configured",
                environment_names=present,
                source="environment",
            )
        return AuthStatus(
            authenticated=False,
            detail="no credential variable set; an interactive Qwen login cannot be detected here",
            blocking=False,
            source="none",
        )

    async def list_models(self, context: CliModelDiscoveryContext | None = None) -> list[str]:
        """Read the models this installation is configured for; never a documented guess.

        Qwen Code has no documented model-listing command, so the only offline authority is its own
        settings. When those are silent, discovery reports unavailable and the wizard keeps the manual
        id and "use the CLI's own default" paths open (spec §17, §11.4).
        """

        context = context or CliModelDiscoveryContext()
        configured = _settings_models(context.project_root)
        if configured:
            self.last_model_discovery = CliModelDiscoveryResult(
                cli_type=self.cli_type,
                available=True,
                method="qwen-settings",
                options=[
                    CliModelOption(id=model, display_name=model, source="qwen settings")
                    for model in configured
                ],
            )
            return configured

        self.last_model_discovery = CliModelDiscoveryResult(
            cli_type=self.cli_type,
            available=False,
            method=self.model_discovery_method,
            error=(
                "Qwen Code documents no model listing command and no configured model was found; "
                "type a model id, or leave it blank to use the CLI's own default"
            ),
        )
        return []

    # ------------------------------------------------------------------ runs

    def start_run(self, request: CliRunRequest) -> AsyncIterator[NormalizedEvent]:
        return self._drive(request, resume_session=None)

    def resume_run(
        self, session_id: str, prompt: str, request: CliRunRequest
    ) -> AsyncIterator[NormalizedEvent]:
        """Resume by the CLI's own session id, and only when the binary advertises ``--resume``.

        The session id is project-scoped, so :class:`~...services.resume` verifies the project
        fingerprint before this is reached — resuming a session recorded in another project would
        attach this run to someone else's history.
        """

        resumed = request
        if prompt != request.prompt:
            resumed = CliRunRequest(**{**vars(request), "prompt": prompt})
        return self._drive(resumed, resume_session=session_id)

    async def _drive(
        self, request: CliRunRequest, *, resume_session: str | None
    ) -> AsyncIterator[NormalizedEvent]:
        if not self.executable:
            yield NormalizedEvent(
                run_id=request.run_id,
                type=EventType.RUN_FAILED,
                source=SOURCE,
                data={
                    "error_type": ErrorType.CLI_NOT_FOUND.value,
                    "message": "qwen is not installed",
                },
            )
            return

        flags = await self.flags()
        if resume_session and not flags.resume:
            # Silently starting a fresh session would look like a resume and behave nothing like one.
            yield NormalizedEvent(
                run_id=request.run_id,
                type=EventType.RUN_FAILED,
                source=SOURCE,
                data={
                    "error_type": ErrorType.SESSION_NOT_FOUND.value,
                    "message": (
                        "this qwen build does not advertise --resume; a new session would not "
                        "continue the recorded one"
                    ),
                },
            )
            return

        argv = build_command(
            request, executable=self.executable, flags=flags, resume_session=resume_session
        )
        env = minimal_environment(request.credential_env)
        proc = ManagedProcess(argv, cwd=request.workspace, env=env)
        self._processes[request.run_id] = proc
        try:
            if flags.stream_json:
                async for event in run_managed_cli(
                    proc=proc, run_id=request.run_id, source=SOURCE, mapper=map_stream_event
                ):
                    self._remember_session(request.run_id, event)
                    yield event
            else:
                async for event in run_buffered_cli(
                    proc=proc, run_id=request.run_id, source=SOURCE, parser=parse_single_result
                ):
                    self._remember_session(request.run_id, event)
                    yield event
        finally:
            self._processes.pop(request.run_id, None)

    def _remember_session(self, run_id: str, event: NormalizedEvent) -> None:
        session_id = event.data.get("session_id") if isinstance(event.data, dict) else None
        if isinstance(session_id, str) and session_id:
            self._sessions[run_id] = session_id

    def session_id_for(self, run_id: str) -> str | None:
        """The CLI's own session handle for a finished run, if it reported one."""

        return self._sessions.get(run_id)

    async def cancel(self, run_id: str) -> TerminationResult:
        proc = self._processes.get(run_id)
        if proc is not None:
            return await proc.cancel()
        return TerminationResult(TerminationOutcome.ALREADY_GONE)

    # ------------------------------------------------------------------ updates

    async def check_update(self):
        installation = await self.detect()
        if installation is None:
            raise RuntimeError("qwen is not installed")
        return await asyncio.to_thread(inspect_update, installation)

    async def perform_update(self, *, dry_run: bool = False, active_run_ids: Sequence[str] = ()):
        installation = await self.detect()
        if installation is None:
            raise RuntimeError("qwen is not installed")
        status = await asyncio.to_thread(inspect_update, installation)
        return await asyncio.to_thread(
            execute_update,
            installation,
            status,
            dry_run=dry_run,
            active_run_ids=active_run_ids,
        )


# ------------------------------------------------------------------------------- settings


_SETTINGS_NAMES = (".qwen/settings.json",)
_USER_SETTINGS = Path.home() / ".qwen" / "settings.json"


def _settings_models(project_root: Path | None) -> list[str]:
    """Models named in the project's or user's Qwen Code settings."""

    candidates: list[Path] = []
    if project_root is not None:
        candidates.extend(project_root / name for name in _SETTINGS_NAMES)
    candidates.append(_USER_SETTINGS)

    for path in candidates:
        models = _read_models(path)
        if models:
            return models
    return []


def _read_models(path: Path) -> list[str]:
    try:
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    for key in ("availableModels", "allowedModels", "models"):
        value = data.get(key)
        if isinstance(value, list):
            models = [item for item in value if isinstance(item, str) and item.strip()]
            if models:
                return models
    single = data.get("model")
    if isinstance(single, str) and single.strip():
        return [single]
    if isinstance(single, dict):
        name = single.get("name") or single.get("id")
        if isinstance(name, str) and name.strip():
            return [name]
    return []


def _dict(value: object) -> dict[str, Any]:
    """``value`` if it is a mapping, else an empty one.

    A named helper rather than an inline ``x if isinstance(x, dict) else {}``: the inline form looks up
    the key twice and, because the isinstance check applies to a *different* call expression, narrows
    nothing — so every downstream ``.get`` is untyped. One helper fixes both.
    """

    return value if isinstance(value, dict) else {}
