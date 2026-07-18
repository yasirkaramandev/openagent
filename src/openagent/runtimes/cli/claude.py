"""Claude Code adapter (spec §8).

Runs ``claude -p --output-format stream-json --verbose`` and maps its JSONL events onto
:class:`NormalizedEvent`s. Permission maps onto ``--allowedTools`` / ``--permission-mode`` from the
profile. Claude is not installed on this machine, so the mapping is validated against recorded
``stream-json`` fixtures (spec §40); the invocation is ready for live use once ``claude`` is present.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ...core.events import EventType, NormalizedEvent
from ...core.models import CliInstallation
from ...core.permissions import get_profile
from ...security.process import (
    ManagedProcess,
    TerminationOutcome,
    TerminationResult,
    minimal_environment,
)
from .base import (
    AuthStatus,
    CliCapabilities,
    CliRunRequest,
    run_managed_cli,
)
from .installations import claude_update_preferences, inspect_installation
from .locator import CliLocation
from .locator import locate_candidates as locate_cli_candidates
from .model_discovery import CliModelDiscoveryResult, discover_claude_models
from .updates import check_update as inspect_update
from .updates import perform_update as execute_update

SOURCE = "claude-cli"


class ClaudeAdapter:
    def __init__(self, executable: str | None = None, *, isolated: bool = False) -> None:
        self._explicit_executable = executable
        self.location: CliLocation = locate_cli_candidates("claude", explicit_path=executable)
        self.executable = executable or self.location.active_executable
        self.last_model_discovery: CliModelDiscoveryResult | None = None
        self.isolated = isolated  # --bare mode (spec §8)
        self._processes: dict[str, ManagedProcess] = {}

    async def detect(self) -> CliInstallation | None:
        self.location = await asyncio.to_thread(self.locate_candidates)
        install = inspect_installation(
            "claude",
            self.location,
            adapter="claude-stream-json",
            **claude_update_preferences(),
        )
        if install is not None:
            self.executable = install.executable
        return install

    def locate_candidates(self) -> CliLocation:
        self.location = locate_cli_candidates("claude", explicit_path=self._explicit_executable)
        return self.location

    async def inspect_installation(self) -> CliInstallation | None:
        return await self.detect()

    async def check_update(self):
        installation = await self.detect()
        if installation is None:
            raise RuntimeError("Claude Code is not installed")
        return await asyncio.to_thread(inspect_update, installation)

    async def perform_update(self, *, dry_run: bool = False, active_run_ids=()):
        installation = await self.detect()
        if installation is None:
            raise RuntimeError("Claude Code is not installed")
        status = await asyncio.to_thread(inspect_update, installation)
        return await asyncio.to_thread(
            execute_update,
            installation,
            status,
            dry_run=dry_run,
            active_run_ids=active_run_ids,
        )

    async def inspect_auth(self) -> AuthStatus:
        cfg = Path.home() / ".claude" / ".credentials.json"
        legacy = Path.home() / ".claude.json"
        if cfg.exists() or legacy.exists():
            return AuthStatus(authenticated=True, detail="~/.claude credentials present")
        return AuthStatus(
            authenticated=False, detail="run `claude` to sign in, or set ANTHROPIC_API_KEY"
        )

    async def capabilities(self) -> CliCapabilities:
        return CliCapabilities(
            structured_events=True,
            resumable=True,
            edits_files=True,
            runs_commands=True,
        )

    model_discovery_method = "aliases + account settings"

    async def list_models(self) -> list[str]:
        result = await asyncio.to_thread(discover_claude_models)
        self.last_model_discovery = result
        return result.models

    # ------------------------------------------------------------------ running

    def start_run(self, request: CliRunRequest) -> AsyncIterator[NormalizedEvent]:
        return self._drive(request, self._build_args(request))

    def resume_run(
        self, session_id: str, prompt: str, request: CliRunRequest
    ) -> AsyncIterator[NormalizedEvent]:
        args = self._build_args(request, prompt=prompt) + ["--resume", session_id]
        return self._drive(request, args)

    def _build_args(self, request: CliRunRequest, prompt: str | None = None) -> list[str]:
        profile = get_profile(request.permission_profile)
        args = [
            self.executable or "claude",
            "-p",
            prompt if prompt is not None else request.prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        if request.model:
            args += ["--model", request.model]
        if request.reasoning_effort:
            args += ["--effort", request.reasoning_effort]
        if profile.claude_allowed_tools:
            args += ["--allowedTools", ",".join(profile.claude_allowed_tools)]
        if profile.claude_permission_mode:
            args += ["--permission-mode", profile.claude_permission_mode]
        if self.isolated:
            args.append("--bare")
        return args

    async def _drive(
        self, request: CliRunRequest, args: list[str]
    ) -> AsyncIterator[NormalizedEvent]:
        if not self.executable:
            yield NormalizedEvent(
                run_id=request.run_id,
                type=EventType.RUN_FAILED,
                source=SOURCE,
                data={"error_type": "cli_not_found", "message": "claude is not installed"},
            )
            return
        env = minimal_environment(request.credential_env)
        proc = ManagedProcess(args, cwd=request.workspace, env=env)
        self._processes[request.run_id] = proc
        try:
            async for event in run_managed_cli(
                proc=proc, run_id=request.run_id, source=SOURCE, mapper=map_claude_event
            ):
                yield event
        finally:
            self._processes.pop(request.run_id, None)

    async def cancel(self, run_id: str) -> TerminationResult:
        proc = self._processes.get(run_id)
        if proc is not None:
            return await proc.cancel()
        return TerminationResult(TerminationOutcome.ALREADY_GONE)


def _parse_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def map_claude_event(obj: dict[str, Any], run_id: str) -> list[NormalizedEvent]:
    """Map one Claude ``stream-json`` object to NormalizedEvents (pure, unit-tested)."""

    etype = obj.get("type", "")

    def ev(t: EventType, **data: Any) -> NormalizedEvent:
        return NormalizedEvent(run_id=run_id, type=t, source=SOURCE, data=data)

    if etype == "system" and obj.get("subtype") == "init":
        return [ev(EventType.SESSION_CREATED, provider_session_id=obj.get("session_id"))]
    if etype == "assistant":
        return _map_assistant(obj.get("message", {}), ev)
    if etype == "user":
        # tool_result blocks come back as a user message
        events = []
        for block in _content_blocks(obj.get("message", {})):
            if block.get("type") == "tool_result":
                events.append(ev(EventType.TOOL_COMPLETED, tool_use_id=block.get("tool_use_id")))
        return events
    if etype == "stream_event":
        return _map_stream_event(obj.get("event", {}), ev)
    if etype == "result":
        return _map_result(obj, ev)
    return [ev(EventType.LOG, raw_type=etype)]


def _map_assistant(message: dict[str, Any], ev) -> list[NormalizedEvent]:
    events: list[NormalizedEvent] = []
    for block in _content_blocks(message):
        btype = block.get("type")
        if btype == "text" and block.get("text"):
            events.append(ev(EventType.MESSAGE_COMPLETED, text=block["text"]))
        elif btype == "tool_use":
            events.append(
                ev(
                    EventType.TOOL_REQUESTED,
                    tool=block.get("name", ""),
                    tool_use_id=block.get("id"),
                )
            )
    return events


def _map_stream_event(event: dict[str, Any], ev) -> list[NormalizedEvent]:
    if event.get("type") == "content_block_delta":
        delta = event.get("delta", {})
        if delta.get("type") == "text_delta" and delta.get("text"):
            return [ev(EventType.MESSAGE_DELTA, text=delta["text"])]
    return []


def _map_result(obj: dict[str, Any], ev) -> list[NormalizedEvent]:
    usage = obj.get("usage") or {}
    # Normalize Claude's native ``total_cost_usd`` onto the single ``provider_cost`` field (item 12).
    events = [
        ev(
            EventType.USAGE_UPDATED,
            input_tokens=usage.get("input_tokens", 0),
            cached_input_tokens=usage.get("cache_read_input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            provider_cost=obj.get("total_cost_usd"),
        )
    ]
    events.append(_result_terminal(obj, ev))
    return events


def _result_terminal(obj: dict[str, Any], ev) -> NormalizedEvent:
    """Decide the terminal event for a Claude ``result`` object — fail closed (items 7, 10).

    A run completes **only** on a well-formed, self-consistent success envelope:

    * ``subtype == "success"`` with ``is_error`` not ``True`` and a string ``result``; or
    * ``is_error is False`` with **no** subtype and a string ``result``.

    Everything else fails closed: an explicit error (``is_error True`` or an ``error*`` subtype), a
    **conflict** (``subtype=="success"`` with ``is_error True``, or an ``error*`` subtype with
    ``is_error False``), a missing/absent ``is_error``, an unknown subtype, a missing or
    wrong-typed ``result``, or an empty object.
    """

    subtype = obj.get("subtype")
    is_error = obj.get("is_error")
    result = obj.get("result")
    result_ok = isinstance(result, str)  # present and a string (absent/dict/number is not valid)

    # Any explicit error signal wins — including a conflict (error subtype while is_error is False,
    # or is_error True while subtype claims success).
    if is_error is True or (isinstance(subtype, str) and subtype.startswith("error")):
        return ev(
            EventType.RUN_FAILED,
            error_type="claude_error",
            message=(result if isinstance(result, str) and result else None)
            or f"claude reported {subtype or 'error'}",
        )

    if subtype == "success":
        if result_ok:
            return ev(EventType.RUN_COMPLETED, result=result)
        return ev(
            EventType.RUN_FAILED,
            error_type="malformed_result",
            message="claude reported success but no valid result string",
        )
    if is_error is False and subtype is None:
        if result_ok:
            return ev(EventType.RUN_COMPLETED, result=result)
        return ev(
            EventType.RUN_FAILED,
            error_type="malformed_result",
            message="claude is_error=false but no valid result string",
        )

    # Neither an explicit success nor an explicit error: ambiguous/unknown -> fail closed.
    return ev(
        EventType.RUN_FAILED,
        error_type="ambiguous_result",
        message="claude result was ambiguous (no explicit success/is_error)",
    )


def _content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []
