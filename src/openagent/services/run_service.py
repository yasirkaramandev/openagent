"""Run orchestration (spec §27, §28, §35, §45).

Ties everything together: allocate a run id, snapshot + create an isolated worktree, dispatch to the
API loop or a CLI adapter, stream normalized events to ``events.jsonl`` (+ SQLite index), collect the
diff/tests, write the standard artifact bundle, and set the final status.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.events import EventType, NormalizedEvent
from ..core.models import AgentProfile, Run, RunStatus, RuntimeType
from ..core.permissions import get_profile
from ..reporting.artifacts import ArtifactWriter, RunArtifacts, TestSummary
from ..runtimes.api_agent.loop import run_api_agent
from ..runtimes.cli.base import CliRunRequest
from ..runtimes.cli.registry import build_cli_adapter
from ..security.approvals import ApprovalGate
from ..security.process import is_pid_alive
from ..storage.event_log import EventLog
from ..tools.base import ToolContext
from ..tools.registry import ToolExecutor
from ..workspaces.worktree import WorktreeManager

if TYPE_CHECKING:
    from ..app import OpenAgentApp

EventHook = Callable[[NormalizedEvent], None]

_ACTIVE = {RunStatus.QUEUED, RunStatus.STARTING, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL}


class RunError(RuntimeError):
    pass


def _new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RunService:
    def __init__(self, app: OpenAgentApp) -> None:
        self.app = app
        self.repos = app.repos
        self.paths = app.paths

    # ------------------------------------------------------------------ CRUD

    def create(
        self, *, agent_name: str, prompt: str, worktree: str = "auto",
        permission_profile: str | None = None,
    ) -> Run:
        agent = self.repos.agents.get(agent_name)
        if not agent:
            raise RunError(f"agent {agent_name!r} not found")
        profile = permission_profile or agent.permission_profile
        get_profile(profile)  # validate
        run = Run(
            id=_new_run_id(), agent=agent_name, prompt=prompt,
            workspace=str(self.paths.project_root), permission_profile=profile,
        )
        run.worktree = worktree  # strategy marker until the workspace is created
        self.repos.runs.upsert(run)
        return run

    def get(self, run_id: str) -> Run | None:
        return self.repos.runs.get(run_id)

    def list(self, limit: int = 50) -> list[Run]:
        return self.repos.runs.list(limit)

    # ------------------------------------------------------------------ execution

    async def execute(self, run: Run, on_event: EventHook | None = None) -> Run:
        agent = self.repos.agents.get(run.agent)
        if not agent:
            raise RunError(f"agent {run.agent!r} not found")

        strategy = run.worktree or "auto"
        run.status = RunStatus.STARTING
        self.repos.runs.upsert(run)

        wt = WorktreeManager(self.paths.project_root, self.paths.worktrees_dir)
        workspace = wt.create(run.id, use_worktree=strategy != "none")
        run.worktree = str(workspace.root)
        run.branch = workspace.branch
        run.base_commit = workspace.base_commit
        run.status = RunStatus.RUNNING
        self.repos.runs.upsert(run)

        run_dir = self.paths.run_dir(run.id)
        event_log = EventLog(run_dir, index=self.repos.event_index)
        writer = ArtifactWriter(run_dir)
        writer.write_request(run)
        art = RunArtifacts()
        if workspace.lower_safety:
            art.warnings.append("Ran in a non-git copy (lower safety): changes are not versioned.")

        state = {"terminal": None}  # RunStatus | None

        def sink(event: NormalizedEvent) -> None:
            saved = event_log.append(event)
            _capture(saved, art, run, state)
            if on_event is not None:
                on_event(saved)

        sink(NormalizedEvent(run_id=run.id, type=EventType.RUN_STARTED, source="openagent",
                             data={"agent": run.agent, "workspace": str(workspace.root)}))

        try:
            rtype = agent.runtime.type
            if rtype is RuntimeType.API_AGENT or rtype == RuntimeType.API_AGENT.value:
                await self._run_api(run, agent, workspace.root, sink, art, state)
            else:
                await self._run_cli(run, agent, workspace.root, sink, state)
        except Exception as exc:  # noqa: BLE001 - convert to a failed run
            state["terminal"] = RunStatus.FAILED
            run.failure_type = run.failure_type or "unknown"
            art.warnings.append(f"runtime error: {exc}")

        # Collect diff + changed files from the worktree.
        art.diff = wt.diff(workspace)
        art.files_changed = wt.changed_files(workspace)
        run.files_changed = art.files_changed

        final = state["terminal"] or RunStatus.COMPLETED
        run.status = final
        run.completed_at = _now()
        if not art.summary and final is RunStatus.COMPLETED:
            art.summary = "Run completed."
        self.repos.runs.upsert(run)

        # Emit our own terminal event only if the runtime didn't already.
        if state.get("emitted_terminal") is not True:
            term_type = EventType.RUN_COMPLETED if final is RunStatus.COMPLETED else EventType.RUN_FAILED
            sink(NormalizedEvent(run_id=run.id, type=term_type, source="openagent",
                                 data={"status": final.value}))

        writer.write_status(run)
        writer.write_results(run, art)
        return run

    async def _run_api(self, run, agent, root: Path, sink, art, state) -> None:
        provider = self.repos.providers.get_by_name(agent.runtime.provider or "")
        if not provider:
            raise RunError(f"provider {agent.runtime.provider!r} not found")
        adapter = self.app.providers.adapter_for(provider)
        profile = get_profile(run.permission_profile)

        def tool_emit(name: str, data: dict) -> None:
            try:
                etype = EventType(name)
            except ValueError:
                etype = EventType.LOG
            sink(NormalizedEvent(run_id=run.id, type=etype, source="api-agent", data=data))

        ctx = ToolContext(
            workspace_root=root, profile=profile,
            approval_gate=ApprovalGate(auto_approve=not profile.require_approval_for_destructive),
            run_id=run.id, emit=tool_emit,
        )
        executor = ToolExecutor(ctx)
        try:
            outcome = await run_api_agent(
                run_id=run.id, agent=agent, prompt=run.prompt, adapter=adapter,
                executor=executor, workspace_root=root, emit=sink,
            )
        finally:
            transport = getattr(adapter, "transport", None)
            if transport is not None:
                await transport.aclose()

        art.summary = outcome.summary or art.summary
        art.usage = outcome.usage.model_dump()
        if outcome.completed:
            state["terminal"] = RunStatus.COMPLETED
        else:
            state["terminal"] = RunStatus.FAILED
            run.failure_type = outcome.error_type or "unknown"

    async def _run_cli(self, run, agent: AgentProfile, root: Path, sink, state) -> None:
        adapter = build_cli_adapter(agent.runtime.cli or "")
        request = CliRunRequest(
            run_id=run.id, prompt=run.prompt, workspace=root,
            permission_profile=run.permission_profile,
        )
        async for event in adapter.start_run(request):
            sink(event)
            if event.data.get("pid") and run.pid is None:
                run.pid = event.data["pid"]

    # ------------------------------------------------------------------ resume / cancel

    async def resume(self, run_id: str, prompt: str, on_event: EventHook | None = None) -> Run:
        run = self.get(run_id)
        if not run:
            raise RunError(f"run {run_id!r} not found")
        agent = self.repos.agents.get(run.agent)
        if not agent or agent.runtime.type not in (RuntimeType.CLI, RuntimeType.CLI.value):
            raise RunError("resume is currently supported for CLI agents only")
        if not run.provider_session_id:
            raise RunError("no session id recorded for this run")

        run_dir = self.paths.run_dir(run.id)
        event_log = EventLog(run_dir, index=self.repos.event_index)
        state = {"terminal": None}
        art = RunArtifacts()

        def sink(event: NormalizedEvent) -> None:
            saved = event_log.append(event)
            _capture(saved, art, run, state)
            if on_event is not None:
                on_event(saved)

        adapter = build_cli_adapter(agent.runtime.cli or "")
        request = CliRunRequest(
            run_id=run.id, prompt=prompt, workspace=Path(run.worktree or run.workspace),
            permission_profile=run.permission_profile, session_id=run.provider_session_id,
        )
        sink(NormalizedEvent(run_id=run.id, type=EventType.SESSION_RESUMED, source="openagent",
                             data={"session_id": run.provider_session_id}))
        async for event in adapter.resume_run(run.provider_session_id, prompt, request):
            sink(event)
        run.status = state["terminal"] or RunStatus.COMPLETED
        run.completed_at = _now()
        self.repos.runs.upsert(run)
        return run

    async def cancel(self, run_id: str) -> None:
        run = self.get(run_id)
        if not run:
            return
        agent = self.repos.agents.get(run.agent)
        if agent and agent.runtime.type in (RuntimeType.CLI, RuntimeType.CLI.value) and agent.runtime.cli:
            adapter = build_cli_adapter(agent.runtime.cli)
            await adapter.cancel(run_id)
        run.status = RunStatus.CANCELLED
        run.completed_at = _now()
        self.repos.runs.upsert(run)

    # ------------------------------------------------------------------ maintenance

    def recover_orphans(self) -> list[str]:
        """Mark active runs whose process is gone as orphaned (spec §45)."""

        recovered: list[str] = []
        for run in self.repos.runs.list_active():
            if not is_pid_alive(run.pid):
                run.status = RunStatus.ORPHANED
                run.completed_at = _now()
                self.repos.runs.upsert(run)
                recovered.append(run.id)
        return recovered

    def output(self, run_id: str, fmt: str = "md") -> str:
        run_dir = self.paths.run_dir(run_id)
        mapping = {
            "md": "output.md", "json": "result.json", "diff": "changes.diff",
            "logs": "logs.txt", "events": "events.jsonl", "handoff": "handoff.md",
            "status": "status.json", "tests": "tests.json",
        }
        name = mapping.get(fmt)
        if name is None:
            raise RunError(f"unknown output format {fmt!r}; choose from {sorted(mapping)}")
        path = run_dir / name
        if not path.exists():
            raise RunError(f"no {fmt} artifact for run {run_id}")
        return path.read_text(encoding="utf-8")


def _capture(event: NormalizedEvent, art: RunArtifacts, run: Run, state: dict) -> None:
    etype = event.type if isinstance(event.type, str) else event.type.value
    data = event.data
    if etype == EventType.SESSION_CREATED.value and data.get("provider_session_id"):
        run.provider_session_id = data["provider_session_id"]
    elif etype == EventType.MESSAGE_COMPLETED.value and data.get("text"):
        art.summary = data["text"]
    elif etype == EventType.TEST_COMPLETED.value:
        art.tests = TestSummary(
            ran=True, passed=data.get("passed"), exit_code=data.get("exit_code"),
            command=data.get("command", ""),
        )
    elif etype in (EventType.FILE_CREATED.value, EventType.FILE_MODIFIED.value,
                   EventType.FILE_DELETED.value):
        verb = etype.split(".")[1]
        art.changes.append(f"{verb} {data.get('path', '')}".strip())
    elif etype in (EventType.COMMAND_COMPLETED.value, EventType.LOG.value):
        if data.get("stderr"):
            art.log_lines.append(str(data["stderr"]))
        if data.get("command"):
            art.log_lines.append(f"$ {data['command']} -> exit {data.get('exit_code')}")
    elif etype == EventType.USAGE_UPDATED.value:
        art.usage = {k: data.get(k) for k in ("input_tokens", "cached_input_tokens", "output_tokens")}
    elif etype == EventType.RUN_COMPLETED.value:
        state["terminal"] = RunStatus.COMPLETED
        state["emitted_terminal"] = True
    elif etype == EventType.RUN_FAILED.value:
        state["terminal"] = RunStatus.FAILED
        state["emitted_terminal"] = True
        run.failure_type = data.get("error_type") or run.failure_type or "unknown"
