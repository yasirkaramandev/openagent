"""Antigravity CLI adapter (Google Antigravity ``agy``).

Verified live against **agy v1.1.0** on 2026-07-13 (see ``tests/fixtures/antigravity_print.jsonl``):

    agy --print "<prompt>" --output-format json [--conversation <id>]

emits a **single** JSON result object (not a streaming feed)::

    {"conversation_id": "...", "status": "SUCCESS", "response": "...",
     "duration_seconds": 3.4, "num_turns": 1,
     "usage": {"input_tokens": ..., "output_tokens": ..., "thinking_tokens": ..., "total_tokens": ...}}

Confirmed live: ``--print`` non-interactive execution (exit 0 on SUCCESS), ``--conversation <id>``
resume (preserves ``conversation_id``, increments ``num_turns``, retains memory), and ``agy models``
auth. The ``status`` enum seen in the binary is ``SUCCESS`` / ``ABORTED`` / ``CANCELLED`` / ``UNKNOWN``
with ``error`` / ``message`` fields on failure. Antigravity reports **no monetary cost** (subscription),
so ``provider_cost`` stays ``None``, and — because the output is a single final object — only coarse
events are available (final text + usage + terminal status), never per-file/per-command events.

Because the output is one object, the mapper is fail-closed: only an explicit ``SUCCESS`` completes;
``CANCELLED`` maps to cancelled; anything else (``ABORTED`` / ``UNKNOWN`` / missing / error) fails. The
shared ``run_managed_cli`` finalizer then reconciles that against the process exit code (spec §6.2).
"""

from __future__ import annotations

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

SOURCE = "antigravity-cli"


class AntigravityAdapter:
    def __init__(self, executable: str | None = None) -> None:
        # The official executable is ``agy``; ``antigravity`` is accepted as an alias if present.
        self.executable = executable or find_executable("agy", "antigravity")
        self._processes: dict[str, ManagedProcess] = {}

    async def detect(self) -> CliInstallation | None:
        if not self.executable:
            return None
        return CliInstallation(
            id="cli_antigravity", type="antigravity", executable=self.executable,
            version=detect_version(self.executable), adapter="antigravity-json",
            authenticated=None, experimental=False,
        )

    async def inspect_auth(self) -> AuthStatus:
        # Offline best-effort: the antigravity-cli state file indicates a configured/signed-in CLI.
        state = Path.home() / ".gemini" / "antigravity-cli" / "jetski_state.pbtxt"
        if state.exists():
            return AuthStatus(authenticated=True, detail="~/.gemini/antigravity-cli state present")
        return AuthStatus(authenticated=False, detail="run `agy` to sign in")

    async def capabilities(self) -> CliCapabilities:
        # Structured JSON result + resume are verified live; edits/commands go through Antigravity's
        # own tools, which the single-object output does not itemize.
        return CliCapabilities(
            structured_events=True, resumable=True, edits_files=True, runs_commands=True,
            experimental=False,
        )

    # ------------------------------------------------------------------ running

    def start_run(self, request: CliRunRequest) -> AsyncIterator[NormalizedEvent]:
        return self._drive(request, self._build_args(request, request.prompt))

    def resume_run(
        self, session_id: str, prompt: str, request: CliRunRequest
    ) -> AsyncIterator[NormalizedEvent]:
        return self._drive(request, self._build_args(request, prompt, conversation=session_id))

    def _build_args(
        self, request: CliRunRequest, prompt: str, *, conversation: str | None = None
    ) -> list[str]:
        args = [self.executable or "agy", "--print", prompt, "--output-format", "json"]
        if conversation:
            args += ["--conversation", conversation]
        args += self._permission_args(request.permission_profile)
        return args

    def _permission_args(self, profile_name: str) -> list[str]:
        """Map the permission profile onto Antigravity's flags.

        A read-only profile runs in ``--mode plan`` (no edits). An editing profile must
        auto-approve, since ``--print`` is non-interactive and cannot answer a tool prompt; the run
        is already isolated in an OpenAgent worktree (same rationale as Codex ``workspace-write`` /
        Claude ``acceptEdits``). The exact non-interactive tool-permission behavior is not itself
        live-verified.
        """

        profile = get_profile(profile_name)
        if not profile.can_edit_files:
            return ["--mode", "plan"]
        return ["--dangerously-skip-permissions"]

    async def _drive(
        self, request: CliRunRequest, args: list[str]
    ) -> AsyncIterator[NormalizedEvent]:
        if not self.executable:
            yield NormalizedEvent(
                run_id=request.run_id, type=EventType.RUN_FAILED, source=SOURCE,
                data={"error_type": "cli_not_found", "message": "antigravity (agy) is not installed"},
            )
            return
        env = minimal_environment(request.credential_env)
        proc = ManagedProcess(args, cwd=request.workspace, env=env)
        self._processes[request.run_id] = proc
        try:
            async for event in run_managed_cli(
                proc=proc, run_id=request.run_id, source=SOURCE, mapper=map_antigravity_event
            ):
                yield event
        finally:
            self._processes.pop(request.run_id, None)

    async def cancel(self, run_id: str) -> None:
        proc = self._processes.get(run_id)
        if proc is not None:
            await proc.cancel()


def map_antigravity_event(obj: dict[str, Any], run_id: str) -> list[NormalizedEvent]:
    """Map Antigravity's single ``--output-format json`` result object to NormalizedEvents (pure).

    Fail-closed on ``status``: only ``SUCCESS`` completes; ``CANCELLED`` cancels; everything else
    (``ABORTED`` / ``UNKNOWN`` / missing / an ``error``) fails. The exit-code reconciliation in
    ``run_managed_cli`` is the second safety net.
    """

    def ev(t: EventType, **data: Any) -> NormalizedEvent:
        return NormalizedEvent(run_id=run_id, type=t, source=SOURCE, data=data)

    events: list[NormalizedEvent] = []
    conversation = obj.get("conversation_id")
    if conversation:
        events.append(ev(EventType.SESSION_CREATED, provider_session_id=conversation))

    response = obj.get("response")
    if isinstance(response, str) and response.strip():
        events.append(ev(EventType.MESSAGE_COMPLETED, text=response))

    usage = obj.get("usage")
    if isinstance(usage, dict):
        events.append(ev(
            EventType.USAGE_UPDATED,
            input_tokens=int(usage.get("input_tokens") or 0),
            cached_input_tokens=0,  # Antigravity reports no cached tokens
            output_tokens=int(usage.get("output_tokens") or 0),
            provider_cost=None,  # subscription product — no per-run monetary cost is reported
        ))

    status = str(obj.get("status") or "").upper()
    if status == "SUCCESS":
        events.append(ev(EventType.RUN_COMPLETED, result=response if isinstance(response, str) else ""))
    elif status == "CANCELLED":
        events.append(ev(EventType.RUN_CANCELLED,
                         reason=str(obj.get("message") or "antigravity reported cancelled")))
    else:
        message = obj.get("error") or obj.get("message") or f"antigravity reported {status or 'no status'}"
        events.append(ev(EventType.RUN_FAILED, error_type="antigravity_error", message=str(message)))
    return events
