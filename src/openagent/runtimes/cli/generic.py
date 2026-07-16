"""Manifest-driven generic CLI adapter (spec §11).

Lets new CLIs be added by data instead of code: a manifest names the executable, the run command
template (with a ``{prompt}`` placeholder), output format, and capabilities. Text-only CLIs are
observed via the workspace diff rather than a structured event stream (spec §10 level 1).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from ...core.events import EventType, NormalizedEvent
from ...core.models import CliInstallation
from ...security.process import (
    ManagedProcess,
    TerminationOutcome,
    TerminationResult,
    minimal_environment,
)
from .base import AuthStatus, CliCapabilities, CliRunRequest, detect_version, find_executable


@dataclass
class CliManifest:
    id: str
    executable_names: list[str]
    run_template: list[str]  # tokens; "{prompt}" is substituted
    output_format: str = "text"  # text | jsonl
    resume_supported: bool = False
    edits_files: bool = True
    runs_commands: bool = True
    experimental: bool = True
    env: dict[str, str] = field(default_factory=dict)


class GenericCliAdapter:
    def __init__(self, manifest: CliManifest, executable: str | None = None) -> None:
        self.manifest = manifest
        self.executable = executable or find_executable(*manifest.executable_names)
        self._processes: dict[str, ManagedProcess] = {}

    async def detect(self) -> CliInstallation | None:
        if not self.executable:
            return None
        return CliInstallation(
            id=f"cli_{self.manifest.id}",
            type=self.manifest.id,
            executable=self.executable,
            version=detect_version(self.executable),
            adapter="generic",
            experimental=self.manifest.experimental,
        )

    async def inspect_auth(self) -> AuthStatus:
        return AuthStatus(
            authenticated=bool(self.executable), detail="assumed via existing CLI login"
        )

    async def capabilities(self) -> CliCapabilities:
        return CliCapabilities(
            structured_events=self.manifest.output_format == "jsonl",
            resumable=self.manifest.resume_supported,
            edits_files=self.manifest.edits_files,
            runs_commands=self.manifest.runs_commands,
            experimental=self.manifest.experimental,
        )

    def start_run(self, request: CliRunRequest) -> AsyncIterator[NormalizedEvent]:
        return self._drive(request)

    def resume_run(
        self, session_id: str, prompt: str, request: CliRunRequest
    ) -> AsyncIterator[NormalizedEvent]:
        raise NotImplementedError(f"{self.manifest.id} does not support resume")

    async def _drive(self, request: CliRunRequest) -> AsyncIterator[NormalizedEvent]:
        if not self.executable:
            yield NormalizedEvent(
                run_id=request.run_id,
                type=EventType.RUN_FAILED,
                source=self.manifest.id,
                data={"error_type": "cli_not_found"},
            )
            return
        args = [
            self.executable if t == "{executable}" else t.replace("{prompt}", request.prompt)
            for t in self.manifest.run_template
        ]
        env = minimal_environment({**self.manifest.env, **request.credential_env})
        proc = ManagedProcess(args, cwd=request.workspace, env=env)
        self._processes[request.run_id] = proc
        await proc.start()
        yield NormalizedEvent(
            run_id=request.run_id,
            type=EventType.RUN_STARTED,
            source=self.manifest.id,
            data={"pid": proc.pid},
        )
        try:
            async for line in proc.stream_stdout():
                if line.strip():
                    yield NormalizedEvent(
                        run_id=request.run_id,
                        type=EventType.MESSAGE_DELTA,
                        source=self.manifest.id,
                        data={"text": line},
                    )
            code = await proc.wait()
            if proc.stdout_limit_exceeded:
                yield NormalizedEvent(
                    run_id=request.run_id,
                    type=EventType.RUN_FAILED,
                    source=self.manifest.id,
                    data={
                        "error_type": "output_limit_exceeded",
                        "message": proc.stdout_limit_detail,
                        "truncated": True,
                        "stdout_bytes": proc.stdout_total_bytes,
                    },
                )
                return
            etype = EventType.RUN_COMPLETED if code == 0 else EventType.RUN_FAILED
            yield NormalizedEvent(
                run_id=request.run_id, type=etype, source=self.manifest.id, data={"exit_code": code}
            )
        finally:
            self._processes.pop(request.run_id, None)

    async def cancel(self, run_id: str) -> TerminationResult:
        proc = self._processes.get(run_id)
        if proc is not None:
            return await proc.cancel()
        return TerminationResult(TerminationOutcome.ALREADY_GONE)


#: Example manifest for a future OpenCode adapter (spec §11).
OPENCODE_MANIFEST = CliManifest(
    id="opencode",
    executable_names=["opencode"],
    run_template=["{executable}", "run", "{prompt}"],
    output_format="text",
    resume_supported=False,
)
