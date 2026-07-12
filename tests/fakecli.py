"""A fake CLI adapter for offline integration tests (spec §40).

Spawns a *real* Python subprocess that emits Codex-style JSONL, so process management, PID capture,
process-tree cancellation, and the terminal-event contract are exercised end to end without any
installed CLI or network. Reuses the production ``map_codex_event`` mapper and
``finalize_terminal_event`` helper so the fake behaves like the real adapters.
"""

from __future__ import annotations

import sys
import textwrap
from collections.abc import AsyncIterator
from pathlib import Path

from openagent.core.events import EventType, NormalizedEvent
from openagent.core.models import CliInstallation
from openagent.runtimes.cli.base import (
    AuthStatus,
    CliCapabilities,
    CliRunRequest,
    finalize_terminal_event,
    is_terminal_event,
)
from openagent.runtimes.cli.codex import _parse_line, map_codex_event
from openagent.security.process import ManagedProcess, minimal_environment

SOURCE = "fake-cli"

# A fake `codex`-like binary. argv[1] selects behavior; it writes the files it claims to change so
# a real worktree diff picks them up.
FAKE_SCRIPT = textwrap.dedent(
    '''
    import json, sys, time

    def emit(obj):
        print(json.dumps(obj), flush=True)

    mode = sys.argv[1] if len(sys.argv) > 1 else "complete"

    if mode == "complete":
        open("new.txt", "w").write("hello from turn 1\\n")
        emit({"type": "thread.started", "thread_id": "th-fake-1"})
        emit({"type": "turn.started"})
        emit({"type": "item.completed", "item": {"item_type": "file_change",
              "changes": [{"kind": "add", "path": "new.txt"}]}})
        emit({"type": "item.completed", "item": {"item_type": "assistant_message",
              "text": "did the thing"}})
        emit({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}})
        sys.exit(0)

    if mode == "resume":
        open("second.txt", "w").write("hello from turn 2\\n")
        emit({"type": "turn.started"})
        emit({"type": "item.completed", "item": {"item_type": "file_change",
              "changes": [{"kind": "add", "path": "second.txt"}]}})
        emit({"type": "item.completed", "item": {"item_type": "assistant_message",
              "text": "second turn done"}})
        emit({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}})
        sys.exit(0)

    if mode == "silent0":
        sys.exit(0)
    if mode == "fail1":
        sys.exit(1)
    if mode == "malformed":
        print("{ this is not valid json", flush=True)
        sys.exit(1)
    if mode == "usage_limit":
        emit({"type": "thread.started", "thread_id": "th-fake-1"})
        emit({"type": "turn.failed", "error": {"message": "You've hit your usage limit."}})
        sys.exit(1)
    if mode == "leak_stderr":
        # Write a secret-looking token to stderr, then fail with no terminal event.
        sys.stderr.write("boom: ghp_stderrLEAK1234567890abcdefGHIJ\\n")
        sys.stderr.flush()
        emit({"type": "thread.started", "thread_id": "th-fake-1"})
        sys.exit(1)
    if mode == "longrun":
        open("started.marker", "w").write("running\\n")
        emit({"type": "thread.started", "thread_id": "th-fake-1"})
        emit({"type": "turn.started"})
        time.sleep(120)
        sys.exit(0)
    '''
)


def write_fake_script(directory: Path) -> Path:
    path = directory / "fake_codex.py"
    path.write_text(FAKE_SCRIPT, encoding="utf-8")
    return path


class FakeCliAdapter:
    """Drives the fake script, mapping its output exactly like the real Codex adapter."""

    def __init__(self, script: Path, *, mode: str = "complete", resume_mode: str = "resume") -> None:
        self.script = script
        self.mode = mode
        self.resume_mode = resume_mode
        self._processes: dict[str, ManagedProcess] = {}

    async def detect(self) -> CliInstallation | None:
        return CliInstallation(id="cli_fake", type="fake", executable=sys.executable)

    async def inspect_auth(self) -> AuthStatus:
        return AuthStatus(authenticated=True, detail="fake")

    async def capabilities(self) -> CliCapabilities:
        return CliCapabilities(structured_events=True, resumable=True, edits_files=True, runs_commands=True)

    def start_run(self, request: CliRunRequest) -> AsyncIterator[NormalizedEvent]:
        return self._drive(request, self.mode)

    def resume_run(
        self, session_id: str, prompt: str, request: CliRunRequest
    ) -> AsyncIterator[NormalizedEvent]:
        return self._drive(request, self.resume_mode)

    async def _drive(self, request: CliRunRequest, mode: str) -> AsyncIterator[NormalizedEvent]:
        proc = ManagedProcess(
            [sys.executable, str(self.script), mode],
            cwd=request.workspace, env=minimal_environment(),
        )
        self._processes[request.run_id] = proc
        await proc.start()
        yield NormalizedEvent(
            run_id=request.run_id, type=EventType.RUN_STARTED, source=SOURCE,
            data={"pid": proc.pid, "create_time": proc.create_time},
        )
        emitted_terminal = False
        try:
            async for line in proc.stream_stdout():
                obj = _parse_line(line)
                if obj is None:
                    continue
                for event in map_codex_event(obj, request.run_id):
                    emitted_terminal = emitted_terminal or is_terminal_event(event)
                    yield event
            code = await proc.wait()
            final = finalize_terminal_event(
                run_id=request.run_id, source=SOURCE, exit_code=code,
                cancelled=proc.cancelled, emitted_terminal=emitted_terminal, stderr=proc.stderr,
            )
            if final is not None:
                yield final
        finally:
            self._processes.pop(request.run_id, None)

    async def cancel(self, run_id: str) -> None:
        proc = self._processes.get(run_id)
        if proc is not None:
            await proc.cancel()
