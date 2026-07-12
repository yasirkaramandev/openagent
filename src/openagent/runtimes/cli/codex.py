"""Codex CLI adapter (spec §7).

Runs ``codex exec --json`` and maps its JSONL thread/turn/item event stream onto
:class:`NormalizedEvent`s. The event schema was confirmed live against ``codex-cli 0.142.5``
(``thread.started`` / ``turn.started`` / ``turn.completed`` / ``turn.failed`` / ``error`` +
``item.*`` events). Raw reasoning is treated as sensitive and never surfaced verbatim (spec §6).
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

SOURCE = "codex-cli"


class CodexAdapter:
    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or find_executable("codex")
        self._processes: dict[str, ManagedProcess] = {}

    async def detect(self) -> CliInstallation | None:
        if not self.executable:
            return None
        version = detect_version(self.executable)
        return CliInstallation(
            id="cli_codex", type="codex", executable=self.executable,
            version=version, adapter="codex-json", authenticated=None,
        )

    async def inspect_auth(self) -> AuthStatus:
        # Codex stores auth under ~/.codex; presence of auth.json indicates a login.
        auth_file = Path.home() / ".codex" / "auth.json"
        if auth_file.exists():
            return AuthStatus(authenticated=True, detail="~/.codex/auth.json present")
        return AuthStatus(authenticated=False, detail="run `codex login` (or set CODEX_API_KEY)")

    async def capabilities(self) -> CliCapabilities:
        return CliCapabilities(
            structured_events=True, resumable=True, edits_files=True, runs_commands=True,
        )

    # ------------------------------------------------------------------ running

    def start_run(self, request: CliRunRequest) -> AsyncIterator[NormalizedEvent]:
        args = self._build_args(request)
        return self._drive(request, args)

    def resume_run(
        self, session_id: str, prompt: str, request: CliRunRequest
    ) -> AsyncIterator[NormalizedEvent]:
        sandbox = get_profile(request.permission_profile).codex_sandbox
        args = [
            self.executable or "codex", "exec", "resume", session_id,
            "--json", "--sandbox", sandbox, prompt,
        ]
        return self._drive(request, args)

    def _build_args(self, request: CliRunRequest) -> list[str]:
        sandbox = get_profile(request.permission_profile).codex_sandbox
        final = str(request.workspace / ".codex-final.txt")
        return [
            self.executable or "codex", "exec", "--json", "--sandbox", sandbox,
            "-o", final, request.prompt,
        ]

    async def _drive(
        self, request: CliRunRequest, args: list[str]
    ) -> AsyncIterator[NormalizedEvent]:
        if not self.executable:
            yield NormalizedEvent(
                run_id=request.run_id, type=EventType.RUN_FAILED, source=SOURCE,
                data={"error_type": "cli_not_found", "message": "codex is not installed"},
            )
            return

        env = minimal_environment(request.credential_env)
        proc = ManagedProcess(args, cwd=request.workspace, env=env)
        self._processes[request.run_id] = proc
        try:
            async for event in run_managed_cli(
                proc=proc, run_id=request.run_id, source=SOURCE, mapper=map_codex_event
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


def map_codex_event(obj: dict[str, Any], run_id: str) -> list[NormalizedEvent]:
    """Map one Codex JSONL object to zero or more NormalizedEvents (pure, unit-tested)."""

    etype = obj.get("type", "")

    def ev(t: EventType, **data: Any) -> NormalizedEvent:
        return NormalizedEvent(run_id=run_id, type=t, source=SOURCE, data=data)

    if etype == "thread.started":
        return [ev(EventType.SESSION_CREATED, provider_session_id=obj.get("thread_id"))]
    if etype == "turn.started":
        return [ev(EventType.MESSAGE_STARTED)]
    if etype == "turn.completed":
        usage = obj.get("usage") or {}
        out = [ev(EventType.USAGE_UPDATED,
                  input_tokens=usage.get("input_tokens", 0),
                  cached_input_tokens=usage.get("cached_input_tokens", 0),
                  output_tokens=usage.get("output_tokens", 0))]
        out.append(ev(EventType.RUN_COMPLETED))
        return out
    if etype == "turn.failed":
        error = obj.get("error") or {}
        return [ev(EventType.RUN_FAILED, message=error.get("message", "turn failed"))]
    if etype == "error":
        return [ev(EventType.LOG, level="error", message=obj.get("message", ""))]
    if etype in ("item.started", "item.updated", "item.completed"):
        return _map_item(obj, etype, ev)
    return [ev(EventType.LOG, raw_type=etype)]


def _map_item(obj: dict[str, Any], etype: str, ev) -> list[NormalizedEvent]:
    item = obj.get("item") or {}
    itype = item.get("item_type") or item.get("type") or ""
    completed = etype == "item.completed"

    if itype in ("assistant_message", "agent_message"):
        if completed:
            return [ev(EventType.MESSAGE_COMPLETED, text=item.get("text", ""))]
        return []
    if itype == "reasoning":
        # Sensitive: never surface raw chain-of-thought (spec §6). Status only.
        return [ev(EventType.LOG, kind="reasoning", status="in_progress")] if not completed else []
    if itype in ("command_execution", "command"):
        if etype == "item.started":
            return [ev(EventType.COMMAND_STARTED, command=item.get("command", ""))]
        if completed:
            return [ev(EventType.COMMAND_COMPLETED,
                       command=item.get("command", ""),
                       exit_code=item.get("exit_code"))]
        return []
    if itype in ("file_change", "patch", "file_update"):
        if completed:
            events = []
            for change in item.get("changes", []) or []:
                kind = (change.get("kind") or change.get("type") or "modify").lower()
                mapping = {
                    "add": EventType.FILE_CREATED, "create": EventType.FILE_CREATED,
                    "delete": EventType.FILE_DELETED, "remove": EventType.FILE_DELETED,
                }
                events.append(ev(mapping.get(kind, EventType.FILE_MODIFIED),
                                 path=change.get("path", "")))
            return events or [ev(EventType.FILE_MODIFIED, path=item.get("path", ""))]
        return []
    if itype in ("mcp_tool_call", "tool_call"):
        if completed:
            return [ev(EventType.TOOL_COMPLETED, tool=item.get("tool", item.get("name", "")))]
        if etype == "item.started":
            return [ev(EventType.TOOL_STARTED, tool=item.get("tool", item.get("name", "")))]
        return []
    if completed:
        return [ev(EventType.LOG, item_type=itype)]
    return []
