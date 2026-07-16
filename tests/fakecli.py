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

from openagent.core.events import NormalizedEvent
from openagent.core.models import CliInstallation
from openagent.runtimes.cli.base import (
    AuthStatus,
    CliCapabilities,
    CliRunRequest,
    run_managed_cli,
)
from openagent.runtimes.cli.codex import map_codex_event
from openagent.runtimes.cli.registry import register_cli_adapter
from openagent.security.process import ManagedProcess, minimal_environment

SOURCE = "fake-cli"

# A fake `codex`-like binary. argv[1] selects behavior; it writes the files it claims to change so
# a real worktree diff picks them up.
FAKE_SCRIPT = textwrap.dedent(
    """
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

    if mode == "mutate":
        # Turn 1: modify an existing file, delete an existing file, create a new one.
        import os
        with open("seed.txt", "a") as fh:
            fh.write("turn1 append\\n")
        if os.path.exists("todelete.txt"):
            os.remove("todelete.txt")
        open("created1.txt", "w").write("created in turn1\\n")
        emit({"type": "thread.started", "thread_id": "th-fake-m"})
        emit({"type": "turn.started"})
        emit({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 3}})
        sys.exit(0)
    if mode == "mutate2":
        # Turn 2 (resume): modify the same file again and create another.
        with open("seed.txt", "a") as fh:
            fh.write("turn2 append\\n")
        open("created2.txt", "w").write("created in turn2\\n")
        emit({"type": "turn.started"})
        emit({"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 1}})
        sys.exit(0)
    if mode == "silent0":
        sys.exit(0)
    if mode == "fail1":
        sys.exit(1)
    if mode == "success_exit1":
        # Claims success in the stream, but the process fails -> must be reported as failed.
        emit({"type": "thread.started", "thread_id": "th-fake-1"})
        emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
        sys.exit(1)
    if mode == "double_terminal":
        # completed -> failed, exit 0. Fail-closed: the later failure must win (terminal_conflict).
        emit({"type": "thread.started", "thread_id": "th-fake-1"})
        emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
        emit({"type": "turn.failed", "error": {"message": "second terminal"}})
        sys.exit(0)
    if mode == "fail_then_complete":
        # failed -> completed, exit 0. The earlier failure must still win (fail-closed).
        emit({"type": "thread.started", "thread_id": "th-fake-1"})
        emit({"type": "turn.failed", "error": {"message": "first failure"}})
        emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
        sys.exit(0)
    if mode == "double_complete":
        # completed -> completed, exit 0. Same outcome twice collapses to a single completed.
        emit({"type": "thread.started", "thread_id": "th-fake-1"})
        emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
        emit({"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 2}})
        sys.exit(0)
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
    """
)


def write_fake_script(directory: Path) -> Path:
    path = directory / "fake_codex.py"
    path.write_text(FAKE_SCRIPT, encoding="utf-8")
    return path


def install_fake_cli(monkeypatch, adapter: FakeCliAdapter) -> FakeCliAdapter:
    """Register ``adapter`` in the **real** CLI registry for the duration of a test.

    Registering (rather than only monkeypatching ``build_cli_adapter``) means the fake resolves
    through exactly the path production uses — including run **preflight**, which now refuses to
    start a run whose CLI is unknown, missing, or unauthenticated. A test that bypassed the registry
    would also bypass preflight and would no longer prove the pipeline works end to end.

    The same *instance* is handed to both the registry and the executor, so a test can still inspect
    or cancel it. The registration is removed after every test by the autouse ``_clean_cli_registry``
    fixture in ``conftest``.
    """

    register_cli_adapter(
        "fake",
        lambda executable=None: adapter,
        display_name="Fake CLI",
        status_label="Test fake (offline)",
    )
    monkeypatch.setattr(
        "openagent.services.run_service.build_cli_adapter",
        lambda cli_type, executable=None: adapter,
    )
    return adapter


class FakeCliAdapter:
    """Drives the fake script, mapping its output exactly like the real Codex adapter."""

    def __init__(
        self, script: Path, *, mode: str = "complete", resume_mode: str = "resume"
    ) -> None:
        self.script = script
        self.mode = mode
        self.resume_mode = resume_mode
        #: Preflight resolves an adapter's executable through this attribute, exactly as it does for
        #: the real CLIs — the fake is a real python process, so this is genuinely its executable.
        self.executable = sys.executable
        self._processes: dict[str, ManagedProcess] = {}

    async def detect(self) -> CliInstallation | None:
        return CliInstallation(id="cli_fake", type="fake", executable=sys.executable)

    async def inspect_auth(self) -> AuthStatus:
        return AuthStatus(authenticated=True, detail="fake")

    async def capabilities(self) -> CliCapabilities:
        return CliCapabilities(
            structured_events=True, resumable=True, edits_files=True, runs_commands=True
        )

    def start_run(self, request: CliRunRequest) -> AsyncIterator[NormalizedEvent]:
        return self._drive(request, self.mode)

    def resume_run(
        self, session_id: str, prompt: str, request: CliRunRequest
    ) -> AsyncIterator[NormalizedEvent]:
        return self._drive(request, self.resume_mode)

    async def _drive(self, request: CliRunRequest, mode: str) -> AsyncIterator[NormalizedEvent]:
        proc = ManagedProcess(
            [sys.executable, str(self.script), mode],
            cwd=request.workspace,
            env=minimal_environment(),
        )
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
