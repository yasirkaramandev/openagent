"""Claude Code adapter (spec §8).

Runs ``claude -p --output-format stream-json --verbose`` and maps its JSONL events onto
:class:`NormalizedEvent`s. Permission maps onto ``--allowedTools`` / ``--permission-mode`` from the
profile. Claude is not installed on this machine, so the mapping is validated against recorded
``stream-json`` fixtures (spec §40); the invocation is ready for live use once ``claude`` is present.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ...core.events import EventType, NormalizedEvent
from ...core.models import CliInstallation
from ...core.permissions import get_profile
from ...security.process import ManagedProcess, minimal_environment
from .base import (
    AuthStatus,
    CliCapabilities,
    CliRunRequest,
    detect_version,
    find_executable,
    run_managed_cli,
)

SOURCE = "claude-cli"


class ClaudeAdapter:
    def __init__(self, executable: str | None = None, *, isolated: bool = False) -> None:
        self.executable = executable or find_executable("claude")
        self.isolated = isolated  # --bare mode (spec §8)
        self._processes: dict[str, ManagedProcess] = {}

    async def detect(self) -> CliInstallation | None:
        if not self.executable:
            return None
        return CliInstallation(
            id="cli_claude", type="claude", executable=self.executable,
            version=detect_version(self.executable), adapter="claude-stream-json",
            authenticated=None,
        )

    async def inspect_auth(self) -> AuthStatus:
        cfg = Path.home() / ".claude" / ".credentials.json"
        legacy = Path.home() / ".claude.json"
        if cfg.exists() or legacy.exists():
            return AuthStatus(authenticated=True, detail="~/.claude credentials present")
        return AuthStatus(authenticated=False, detail="run `claude` to sign in, or set ANTHROPIC_API_KEY")

    async def capabilities(self) -> CliCapabilities:
        return CliCapabilities(
            structured_events=True, resumable=True, edits_files=True, runs_commands=True,
        )

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
            self.executable or "claude", "-p", prompt if prompt is not None else request.prompt,
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        ]
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
                run_id=request.run_id, type=EventType.RUN_FAILED, source=SOURCE,
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

    async def cancel(self, run_id: str) -> None:
        proc = self._processes.get(run_id)
        if proc is not None:
            await proc.cancel()


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
            events.append(ev(EventType.TOOL_REQUESTED, tool=block.get("name", ""),
                             tool_use_id=block.get("id")))
    return events


def _map_stream_event(event: dict[str, Any], ev) -> list[NormalizedEvent]:
    if event.get("type") == "content_block_delta":
        delta = event.get("delta", {})
        if delta.get("type") == "text_delta" and delta.get("text"):
            return [ev(EventType.MESSAGE_DELTA, text=delta["text"])]
    return []


def _map_result(obj: dict[str, Any], ev) -> list[NormalizedEvent]:
    usage = obj.get("usage") or {}
    events = [ev(EventType.USAGE_UPDATED,
                 input_tokens=usage.get("input_tokens", 0),
                 cached_input_tokens=usage.get("cache_read_input_tokens", 0),
                 output_tokens=usage.get("output_tokens", 0),
                 cost_usd=obj.get("total_cost_usd"))]
    events.append(_result_terminal(obj, ev))
    return events


def _result_terminal(obj: dict[str, Any], ev) -> NormalizedEvent:
    """Decide the terminal event for a Claude ``result`` object — fail closed (item 7).

    Success is only accepted when Claude *explicitly* says so: ``subtype == "success"``, or
    ``is_error is False``. A missing ``is_error``, an error subtype, an unknown/absent subtype, or
    a malformed result all map to a failure — an ambiguous result is never counted as completed.
    """

    subtype = obj.get("subtype")
    is_error = obj.get("is_error")
    if subtype == "success" or is_error is False:
        return ev(EventType.RUN_COMPLETED, result=obj.get("result", ""))
    if is_error is True or (isinstance(subtype, str) and subtype.startswith("error")):
        return ev(EventType.RUN_FAILED, error_type="claude_error",
                  message=obj.get("result") or f"claude reported {subtype or 'error'}")
    # Neither an explicit success nor an explicit error: treat the ambiguous/malformed result as a
    # failure rather than silently completing.
    return ev(EventType.RUN_FAILED, error_type="ambiguous_result",
              message="claude result was ambiguous (no explicit success/is_error)")


def _content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []
