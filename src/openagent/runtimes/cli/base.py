"""CLI adapter contract + shared helpers (spec §6.2).

A CLI adapter does not build an agent loop; it runs an installed coding CLI as a subprocess and
converts its native output into OpenAgent :class:`NormalizedEvent`s. The five-method Protocol mirrors
spec §6.2.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ...core.events import EventType, NormalizedEvent
from ...core.models import CliInstallation
from ...security.process import ManagedProcess

#: Signature of a pure event mapper (``map_codex_event`` / ``map_claude_event``).
EventMapper = Callable[[dict[str, Any], str], list[NormalizedEvent]]

#: The terminal event types every CLI adapter must resolve a run to exactly one of (spec §6.2).
TERMINAL_EVENT_TYPES = frozenset({
    EventType.RUN_COMPLETED.value,
    EventType.RUN_FAILED.value,
    EventType.RUN_CANCELLED.value,
})


@dataclass
class CliRunRequest:
    run_id: str
    prompt: str
    workspace: Path
    permission_profile: str = "safe-edit"
    #: Credentials to inject only into the child environment (spec §7), e.g. {"CODEX_API_KEY": ...}.
    credential_env: dict[str, str] = field(default_factory=dict)
    session_id: str | None = None


@dataclass
class AuthStatus:
    authenticated: bool
    detail: str = ""


@dataclass
class CliCapabilities:
    structured_events: bool
    resumable: bool
    edits_files: bool
    runs_commands: bool
    experimental: bool = False


@runtime_checkable
class CliAdapter(Protocol):
    """The CLI adapter contract (spec §6.2)."""

    async def detect(self) -> CliInstallation | None: ...

    async def inspect_auth(self) -> AuthStatus: ...

    async def capabilities(self) -> CliCapabilities: ...

    def start_run(self, request: CliRunRequest) -> AsyncIterator[NormalizedEvent]: ...

    def resume_run(self, session_id: str, prompt: str, request: CliRunRequest) -> AsyncIterator[NormalizedEvent]: ...

    async def cancel(self, run_id: str) -> None: ...


def find_executable(*names: str) -> str | None:
    """Locate a CLI on PATH, including the common ``~/.local/bin`` install location."""

    for name in names:
        found = shutil.which(name)
        if found:
            return found
    # Fallback: user-local bin (codex/agy install here and may be off PATH in some shells).
    for name in names:
        candidate = Path.home() / ".local" / "bin" / name
        if candidate.exists():
            return str(candidate)
    return None


def is_terminal_event(event: NormalizedEvent) -> bool:
    etype = event.type if isinstance(event.type, str) else event.type.value
    return etype in TERMINAL_EVENT_TYPES


def parse_json_line(line: str) -> dict[str, Any] | None:
    """Parse one JSONL line to a dict, or ``None`` for blank/invalid/non-object lines."""

    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def reconcile_terminal(
    *,
    run_id: str,
    source: str,
    captured: NormalizedEvent | None,
    exit_code: int | None,
    cancelled: bool,
    stderr: str = "",
) -> NormalizedEvent:
    """Produce the single terminal event for a run, reconciled with the exit code (spec §6.2, §43).

    ``captured`` is the first terminal event the CLI's own stream produced (or ``None``). Rules:

    * a cancellation-in-progress always wins → ``run.cancelled`` (a killed "success" is not success);
    * a stream ``run.completed`` is honored **only** when the process exited cleanly (0/None); a
      non-zero exit turns it into ``run.failed`` (exit code contradicts the success claim);
    * a stream ``run.failed`` / ``run.cancelled`` is kept as-is (a zero exit never rescues it);
    * no terminal event at all → ``run.failed`` (clean-exit-but-no-result or a non-zero exit).
    """

    if cancelled:
        return NormalizedEvent(
            run_id=run_id, type=EventType.RUN_CANCELLED, source=source,
            data={"reason": "cancelled by user", "exit_code": exit_code},
        )
    captured_type = None
    if captured is not None:
        captured_type = captured.type if isinstance(captured.type, str) else captured.type.value

    if captured_type == EventType.RUN_COMPLETED.value:
        if exit_code in (0, None):
            return captured  # type: ignore[return-value]
        return NormalizedEvent(
            run_id=run_id, type=EventType.RUN_FAILED, source=source,
            data={
                "error_type": "exit_code_mismatch", "exit_code": exit_code,
                "message": f"CLI reported success but exited with code {exit_code}",
                "stderr": (stderr or "")[-2000:],
            },
        )
    if captured_type in (EventType.RUN_FAILED.value, EventType.RUN_CANCELLED.value):
        return captured  # type: ignore[return-value]

    clean = exit_code in (0, None)
    detail = "clean exit but no terminal event" if clean else f"exit code {exit_code}"
    return NormalizedEvent(
        run_id=run_id, type=EventType.RUN_FAILED, source=source,
        data={
            "error_type": "no_terminal_event" if clean else "command_failed",
            "exit_code": exit_code,
            "message": f"CLI produced no successful result ({detail})",
            "stderr": (stderr or "")[-2000:],
        },
    )


async def run_managed_cli(
    *, proc: ManagedProcess, run_id: str, source: str, mapper: EventMapper,
) -> AsyncIterator[NormalizedEvent]:
    """Start ``proc``, normalize its JSONL output, and enforce the terminal-state contract.

    Shared by every CLI adapter (codex, claude, and the test fake) so they finalize identically:

    * emits ``run.started`` (with pid/create_time) up front;
    * yields non-terminal events as they stream;
    * **buffers** terminal events — keeps only the first, drops the rest — and after the process
      exits yields exactly one terminal event reconciled against the exit code
      (:func:`reconcile_terminal`). A success event + non-zero exit becomes failed; a killed process
      becomes cancelled; two terminal events collapse to one.
    """

    await proc.start()
    yield NormalizedEvent(
        run_id=run_id, type=EventType.RUN_STARTED, source=source,
        data={"pid": proc.pid, "create_time": proc.create_time},
    )
    captured: NormalizedEvent | None = None
    async for line in proc.stream_stdout():
        obj = parse_json_line(line)
        if obj is None:
            continue
        for event in mapper(obj, run_id):
            if is_terminal_event(event):
                if captured is None:
                    captured = event  # keep the first; later terminal events are dropped
                continue  # never surface a terminal event mid-stream; it's reconciled at the end
            yield event
    code = await proc.wait()
    yield reconcile_terminal(
        run_id=run_id, source=source, captured=captured,
        exit_code=code, cancelled=proc.cancelled, stderr=proc.stderr,
    )


def detect_version(executable: str) -> str | None:
    try:
        result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
        out = (result.stdout or result.stderr).strip()
        return out.splitlines()[0] if out else None
    except (OSError, subprocess.TimeoutExpired):
        return None
