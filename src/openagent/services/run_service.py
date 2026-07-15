"""Run orchestration (spec §27, §28, §35, §45).

Ties everything together: allocate a run id, run **preflight**, snapshot + create an isolated
workspace (per the chosen strategy), dispatch to the API loop or a CLI adapter, stream normalized
events to ``events.jsonl`` (+ SQLite index), collect the diff/tests, write the standard artifact
bundle, and set the final status. PID and session ids are persisted the moment they arrive so a run
is recoverable and cancellable across restarts.

Lifecycle contract (item 4). Every run emits **exactly one** ``run.started`` — this module's, never
an adapter's — then zero or more ``run.phase`` transitions::

    queued → preflight → preparing_workspace → starting_backend → running
           → [waiting_approval | waiting_user] → finalizing → completed | failed | cancelled

…and **exactly one** terminal event. A backend subprocess coming up is a ``process.started`` (with
its pid), which is a different fact from the run starting.

Failures are persisted, never just warned about (item 13): any runtime exception becomes a
``run.failed`` carrying a normalized ``error_type``, a redacted message, the phase it failed in, and
the source — and it lands in ``events.jsonl``, ``status.json``, ``result.json`` and ``output.md``.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.cancellation import CancellationRegistry, RunCancellation, RunCancelled
from ..core.events import EventType, NormalizedEvent, RunPhase
from ..core.models import AgentProfile, Run, RunStatus, RuntimeType, enum_value
from ..core.permissions import get_profile
from ..core.projection import RunProjection
from ..reporting.artifacts import ArtifactWriter, RunArtifacts, TestSummary
from ..runtimes.api_agent.loop import run_api_agent
from ..runtimes.cli.base import CliAdapter, CliRunRequest
from ..runtimes.cli.registry import build_cli_adapter
from ..security.approvals import ApprovalCallback, ApprovalGate
from ..security.process import PID_ALIVE, pid_identity, run_process_status, terminate_pid_tree
from ..storage.event_log import EventLog
from ..tools.base import AskUserResolver, ToolContext
from ..tools.registry import ToolExecutor
from ..workspaces.worktree import NONE, STRATEGIES, WorktreeManager
from .preflight import PreflightReport, PreflightService

if TYPE_CHECKING:
    from ..app import OpenAgentApp

EventHook = Callable[[NormalizedEvent], None]

_ACTIVE = {RunStatus.QUEUED, RunStatus.STARTING, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL}
_TERMINAL = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.ORPHANED}


class RunError(RuntimeError):
    pass


class PreflightFailed(RunError):
    """Preflight found a blocking problem — the run never starts (item 7)."""

    def __init__(self, report: PreflightReport) -> None:
        super().__init__(report.summary())
        self.report = report
        self.error_type = report.error_type


def _new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _status_value(run: Run) -> str:
    return enum_value(run.status)


class RunService:
    def __init__(self, app: OpenAgentApp) -> None:
        self.app = app
        self.repos = app.repos
        self.paths = app.paths
        self.preflight = PreflightService(app)
        #: Live CLI adapters keyed by run_id, so cancel() reaches the *same* instance that owns the
        #: subprocess (spec §45). Absent after a restart → cancel falls back to PID termination.
        self._cli_adapters: dict[str, CliAdapter] = {}
        #: Runs a cancel was requested for — prevents overwriting ``cancelled`` with ``completed``.
        self._cancelled: set[str] = set()
        #: The live cancellation flag for each in-flight run — the API loop's stop signal (item 9).
        self.cancellations = CancellationRegistry()

    # ------------------------------------------------------------------ CRUD

    def create(
        self, *, agent_name: str, prompt: str, worktree: str = "auto",
        permission_profile: str | None = None, confirm_in_place: bool = False,
    ) -> Run:
        agent = self.repos.agents.get(agent_name)
        if not agent:
            raise RunError(f"agent {agent_name!r} not found")
        profile = permission_profile or agent.permission_profile
        prof = get_profile(profile)  # validate
        if worktree not in STRATEGIES:
            raise RunError(f"unknown worktree strategy {worktree!r}; choose from {STRATEGIES}")
        if worktree == NONE and prof.can_edit_files and not confirm_in_place:
            raise RunError(
                "worktree 'none' runs a file-editing agent directly in your project with no "
                "isolation; pass explicit confirmation to proceed"
            )
        run = Run(
            id=_new_run_id(), agent=agent_name, prompt=prompt,
            workspace=str(self.paths.project_root), permission_profile=profile,
            worktree_strategy=worktree,
        )
        self.repos.runs.upsert(run)
        return run

    def get(self, run_id: str) -> Run | None:
        return self.repos.runs.get(run_id)

    def list(self, limit: int = 50) -> Sequence[Run]:
        return self.repos.runs.list(limit)

    # ------------------------------------------------------------------ execution

    async def execute(
        self, run: Run, on_event: EventHook | None = None,
        approval_callback: ApprovalCallback | None = None,
        ask_user_callback: AskUserResolver | None = None,
    ) -> Run:
        agent = self.repos.agents.get(run.agent)
        if not agent:
            raise RunError(f"agent {run.agent!r} not found")

        cancel = self.cancellations.create(run.id)
        cancel.bind()

        run_dir = self.paths.run_dir(run.id)
        art = RunArtifacts()
        projection = RunProjection(run.id)
        state: dict[str, Any] = {"terminal": None}  # RunStatus | None

        # The whole run — setup, execution, and finalization — lives inside one lifecycle boundary
        # (item 9.4). A disk-full during setup, a PermissionError writing an artifact, or a failed
        # terminal-event append must never leave the run "running", and an artifact-write failure
        # must never be mistaken for success. Whatever happens, the run reaches a terminal state, the
        # DB is authoritative, and the *first* real error — not a later cleanup error — is recorded.
        try:
            event_log = EventLog(run_dir, index=self.repos.event_index)
            writer = ArtifactWriter(run_dir)
            writer.write_request(run)

            def sink(event: NormalizedEvent) -> None:
                # ``run.phase`` means "the phase *changed*" (item 4). A backend that re-announces the
                # phase it is already in (Codex sends turn.started for every turn) is not a
                # transition, so it is dropped rather than written to the log twice.
                if _is_redundant_phase(event, run):
                    return
                if _is_terminal_event(event):
                    # The terminal event must be the LAST log entry and the final projection update
                    # (item 1). Every CLI adapter (via run_managed_cli) emits its own terminal event
                    # mid-stream; if we logged it here, the later ``finalizing`` phase + diff would
                    # land *after* it, leaving events[-1] == run.phase. So capture its meaning now
                    # (status, failure, summary) but buffer it — the finalize block writes exactly
                    # one terminal event, last.
                    _capture(event, art, run, state)
                    state["pending_terminal"] = event
                    return
                saved = event_log.append(event)
                projection.apply(saved)
                _capture(saved, art, run, state)  # this is what advances run.phase
                self._persist_progress(saved, run)
                if on_event is not None:
                    on_event(saved)

            def phase(new: RunPhase, **data: Any) -> None:
                sink(NormalizedEvent(run_id=run.id, type=EventType.RUN_PHASE, source="openagent",
                                     data={"phase": new.value, **data}))
                self.repos.runs.upsert(run)

            # The one and only run.started for this run — OpenAgent's, never a backend's (item 4).
            run.status = RunStatus.STARTING
            self.repos.runs.upsert(run)
            sink(NormalizedEvent(
                run_id=run.id, type=EventType.RUN_STARTED, source="openagent",
                data={"agent": run.agent, "workspace": run.workspace,
                      "permission_profile": run.permission_profile,
                      "worktree_strategy": run.worktree_strategy},
            ))

            workspace = None
            wt = WorktreeManager(self.paths.project_root, self.paths.worktrees_dir)
            try:
                # ---- preflight: prove the agent can run before creating anything (item 7) --------
                phase(RunPhase.PREFLIGHT)
                report = await self.preflight.check(
                    agent_name=run.agent, permission_profile=run.permission_profile,
                )
                sink(NormalizedEvent(
                    run_id=run.id, type=EventType.LOG, source="openagent",
                    data={"kind": "preflight", "ok": report.ok,
                          "checks": [c.line() for c in report.checks]},
                ))
                for warning in report.warnings:
                    art.warnings.append(f"preflight: {warning.line()}")
                if not report.ok:
                    raise PreflightFailed(report)
                cancel.check()

                # ---- workspace --------------------------------------------------------------
                phase(RunPhase.PREPARING_WORKSPACE)
                try:
                    workspace = wt.create(run.id, strategy=run.worktree_strategy or "auto")
                except Exception as exc:  # noqa: BLE001 - a workspace failure is its own failure type
                    raise _typed(exc, "workspace_failed") from exc
                run.worktree = str(workspace.root)
                run.worktree_strategy = workspace.strategy
                run.branch = workspace.branch
                run.base_commit = workspace.base_commit
                run.is_copy = workspace.is_copy
                run.in_place = workspace.in_place
                run.source_path = str(workspace.source)
                run.baseline_dir = str(workspace.baseline_dir) if workspace.baseline_dir else None
                self.repos.runs.upsert(run)
                if workspace.lower_safety:
                    art.warnings.append(_lower_safety_warning(workspace))
                sink(NormalizedEvent(
                    run_id=run.id, type=EventType.WORKSPACE_PREPARED, source="openagent",
                    data={"workspace": str(workspace.root), "strategy": workspace.strategy,
                          "branch": workspace.branch, "in_place": workspace.in_place},
                ))
                cancel.check()

                # ---- backend ----------------------------------------------------------------
                phase(RunPhase.STARTING_BACKEND)
                run.status = RunStatus.RUNNING
                self.repos.runs.upsert(run)
                phase(RunPhase.RUNNING)

                rtype = agent.runtime.type
                if rtype is RuntimeType.API_AGENT or rtype == RuntimeType.API_AGENT.value:
                    await self._run_api(run, agent, workspace.root, sink, art, state,
                                        approval_callback, workspace.describe_for_agent(),
                                        ask_user_callback, cancel)
                else:
                    await self._run_cli(run, agent, workspace.root, sink, state, run_dir)
            except RunCancelled as exc:
                state["terminal"] = RunStatus.CANCELLED
                self._cancelled.add(run.id)
                run.failure_type = "user_cancelled"
                if state.get("emitted_terminal") is not True:
                    sink(NormalizedEvent(
                        run_id=run.id, type=EventType.RUN_CANCELLED, source="openagent",
                        data={"reason": exc.reason, "phase": run.phase},
                    ))
            except Exception as exc:  # noqa: BLE001 - every runtime error becomes a persisted failure
                self._fail(run, sink, state, exc)
            finally:
                self.cancellations.discard(run.id)

            # ---- finalize (items 1 + 9.4) -------------------------------------------------
            # The ``finalizing`` phase and the diff happen BEFORE the single terminal event, which is
            # written LAST — so events[-1] is always the terminal event and the projection never
            # settles on "status: completed / phase: finalizing".
            sink(NormalizedEvent(run_id=run.id, type=EventType.RUN_PHASE, source="openagent",
                                 data={"phase": RunPhase.FINALIZING.value}))
            if workspace is not None:
                try:
                    # Collect diff + changed files from the workspace (git worktrees and copies).
                    art.diff = wt.diff(workspace)
                    art.files_changed = wt.changed_files(workspace)
                    run.files_changed = art.files_changed
                except Exception as exc:  # noqa: BLE001 - a finalization failure is its own terminal state
                    # A finalization error invalidates a prior *success* (item 1) but must not mask
                    # an earlier failure/cancellation (item 5): only an otherwise-completing run flips.
                    if state.get("terminal") in (None, RunStatus.COMPLETED):
                        state["terminal"] = RunStatus.FAILED
                        run.failure_type = "finalization_failed"
                        state["pending_terminal"] = None  # the buffered success no longer holds
                        if not art.error:
                            art.error = {"error_type": "finalization_failed",
                                         "message": str(exc) or exc.__class__.__name__,
                                         "phase": RunPhase.FINALIZING.value, "source": "openagent"}

            final = self._resolve_final(run, state)
            run.status = final
            run.phase = final.value if final.value in _PHASE_VALUES else RunPhase.FAILED.value
            run.completed_at = _now()
            if final is RunStatus.COMPLETED:
                run.failure_type = None  # a completed run carries no failure type (item 18)
                if not art.summary:
                    art.summary = projection.final_message or "Run completed."
            self.repos.runs.upsert(run)

            # Reuse the backend's own terminal event when it still matches the resolved outcome
            # (keeps its richer data); otherwise synthesize one for the reconciled status.
            pending = state.get("pending_terminal")
            if pending is not None and _status_of_terminal(pending) is final:
                terminal_event = pending
            else:
                terminal_event = NormalizedEvent(
                    run_id=run.id, type=_terminal_event_type(final), source="openagent",
                    data={"status": final.value} if final is RunStatus.COMPLETED
                    else {"status": final.value, "error_type": run.failure_type},
                )
            # Reflect the terminal outcome in the projection, write the artifact bundle (each file
            # atomically), and only THEN append the terminal event — last in events.jsonl (item 1).
            # Writing artifacts *before* the terminal event means a failed artifact write is caught
            # before the log ever says "done", so result.json can never be missing while the log
            # claims success (item 9.4).
            projection.apply(terminal_event)
            writer.write_status(run)
            writer.write_results(run, art, projection)
            writer.write_timeline(run, projection)
            saved = event_log.append(terminal_event)
            state["terminal_written"] = True
            if on_event is not None:
                with contextlib.suppress(Exception):  # a UI-notify failure must not fail the run
                    on_event(saved)
            return run
        except Exception as exc:  # noqa: BLE001 - the outer lifecycle boundary (item 9.4)
            return self._finalize_failure(run, run_dir, art, state, exc)
        finally:
            self.cancellations.discard(run.id)

    def _finalize_failure(
        self, run: Run, run_dir: Path, art: RunArtifacts, state: dict, exc: Exception
    ) -> Run:
        """Force a terminal state after a setup/finalize error (item 9.4), best-effort throughout.

        A run never stays running, and an artifact-write failure never looks like success. The first
        real error wins (item 5): a run already resolved to failed/cancelled keeps that terminal
        type; one heading for *completed* (or unresolved) is flipped to FAILED, because its result
        could not be finished. The writers are recreated fresh here in case the originals never
        existed, and every recovery step is suppressed so recording the failure cannot itself raise.
        """

        prior = state.get("terminal")
        final = prior if prior in (RunStatus.CANCELLED, RunStatus.FAILED) else RunStatus.FAILED
        if final is RunStatus.FAILED:
            run.failure_type = run.failure_type or "artifact_write_failed"
            if not art.error:
                art.error = {"error_type": run.failure_type,
                             "message": str(exc) or exc.__class__.__name__,
                             "phase": run.phase, "source": "openagent"}
        run.status = final
        run.phase = final.value if final.value in _PHASE_VALUES else RunPhase.FAILED.value
        run.completed_at = _now()
        # The DB is the source of truth for status — persist the terminal state above all else.
        with contextlib.suppress(Exception):
            self.repos.runs.upsert(run)
        # Best-effort audit: exactly one terminal event (only if the normal path never wrote one),
        # then a minimal, explicitly-partial artifact set.
        if not state.get("terminal_written"):
            with contextlib.suppress(Exception):
                EventLog(run_dir, index=self.repos.event_index).append(
                    NormalizedEvent(
                        run_id=run.id, type=_terminal_event_type(final), source="openagent",
                        data={"status": final.value, "error_type": run.failure_type},
                    )
                )
                state["terminal_written"] = True
        with contextlib.suppress(Exception):
            writer = ArtifactWriter(run_dir)
            writer.write_status(run)
            writer.write_results(run, art, None)
        return run

    def _fail(self, run: Run, sink: EventHook, state: dict, exc: Exception) -> None:
        """Persist a runtime exception as a real ``run.failed`` event (item 13).

        The old code only appended a warning to the in-memory artifact bundle and let the run fall
        into a generic terminal status, so ``events.jsonl`` never recorded *why* it failed. Now the
        failure is a first-class event with a normalized type, the phase it happened in, and a safe
        message — and the artifact writers pick it up from there.
        """

        error_type = getattr(exc, "error_type", None) or _classify_exception(exc)
        message = str(exc) or exc.__class__.__name__
        state["terminal"] = RunStatus.FAILED
        run.failure_type = error_type
        sink(NormalizedEvent(
            run_id=run.id, type=EventType.RUN_FAILED, source="openagent",
            data={"error_type": error_type, "message": message, "phase": run.phase,
                  "source": "openagent"},
        ))

    def _resolve_final(self, run: Run, state: dict) -> RunStatus:
        """Never silently default an unknown terminal state to completed (spec §43)."""

        if run.id in self._cancelled or state.get("terminal") is RunStatus.CANCELLED:
            return RunStatus.CANCELLED
        terminal = state.get("terminal")
        if terminal is None:
            run.failure_type = run.failure_type or "no_terminal_event"
            return RunStatus.FAILED
        return terminal

    def _persist_progress(self, event: NormalizedEvent, run: Run) -> None:
        """Persist the run the moment a PID or session id arrives (spec §45 orphan/cancel/resume)."""

        etype = event.type if isinstance(event.type, str) else event.type.value
        if etype in (EventType.RUN_STARTED.value, EventType.PROCESS_STARTED.value,
                     EventType.SESSION_CREATED.value):
            self.repos.runs.upsert(run)

    async def _run_api(self, run, agent, root: Path, sink, art, state,
                       approval_callback: ApprovalCallback | None = None,
                       workspace_note: str = "",
                       ask_user_callback: AskUserResolver | None = None,
                       cancel: RunCancellation | None = None) -> None:
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
            approval_gate=ApprovalGate(
                auto_approve=not profile.require_approval_for_destructive,
                callback=approval_callback, emit=tool_emit, run_id=run.id,
            ),
            run_id=run.id, emit=tool_emit, ask_user_callback=ask_user_callback,
            # A blocking command (run_command/run_tests) polls this so a Cancel kills its process
            # tree mid-run instead of waiting for it to finish (item 9.2).
            cancellation=cancel,
        )
        executor = ToolExecutor(ctx)
        try:
            outcome = await run_api_agent(
                run_id=run.id, agent=agent, prompt=run.prompt, adapter=adapter,
                executor=executor, workspace_root=root, emit=sink, workspace_note=workspace_note,
                cancellation=cancel,
            )
        finally:
            # Always release the HTTP transport — on success, failure, and cancellation alike.
            transport = getattr(adapter, "transport", None)
            if transport is not None:
                await transport.aclose()

        art.summary = outcome.summary or art.summary
        art.usage = outcome.usage.model_dump()
        if outcome.cancelled:
            raise RunCancelled(outcome.error_message or "cancelled by user")
        if outcome.completed:
            state["terminal"] = RunStatus.COMPLETED
        else:
            state["terminal"] = RunStatus.FAILED
            run.failure_type = outcome.error_type or "unknown"
            sink(NormalizedEvent(
                run_id=run.id, type=EventType.RUN_FAILED, source="api-agent",
                data={"error_type": run.failure_type,
                      "message": outcome.error_message or "the API agent did not complete",
                      "phase": run.phase, "source": "api-agent"},
            ))

    async def _run_cli(self, run, agent: AgentProfile, root: Path, sink, state,
                       run_dir: Path) -> None:
        adapter = build_cli_adapter(agent.runtime.cli or "")
        self._cli_adapters[run.id] = adapter
        request = CliRunRequest(
            run_id=run.id, prompt=run.prompt, workspace=root,
            permission_profile=run.permission_profile,
            # Scratch files a CLI needs (Codex's --output-last-message) belong to OpenAgent, not to
            # the user's project — keep them out of the workspace and out of the diff (item 6).
            artifacts_dir=run_dir,
            model=agent.runtime.model or None,
        )
        try:
            async for event in adapter.start_run(request):
                sink(event)
        finally:
            self._cli_adapters.pop(run.id, None)

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
        turn_state: dict[str, Any] = {"terminal": None}
        # Per-turn artifacts (item 18): captured live so turn_NNN.md holds only THIS turn's summary,
        # usage, and tests — the cumulative view is rebuilt separately for result.json.
        turn_art = RunArtifacts()
        event_start = sum(1 for _ in event_log.read()) + 1  # 1-based index of this turn's first event

        # Mark the run active again while resuming; append (never truncate) the event log.
        run.status = RunStatus.RUNNING
        run.turns += 1
        run.completed_at = None
        self.repos.runs.upsert(run)

        def sink(event: NormalizedEvent) -> None:
            saved = event_log.append(event)
            _capture(saved, run=run, art=turn_art, state=turn_state)
            self._persist_progress(saved, run)
            if on_event is not None:
                on_event(saved)

        adapter = build_cli_adapter(agent.runtime.cli or "")
        self._cli_adapters[run.id] = adapter
        cancel = self.cancellations.create(run.id)
        cancel.bind()
        workspace_root = Path(run.worktree or run.workspace)
        request = CliRunRequest(
            run_id=run.id, prompt=prompt, workspace=workspace_root,
            permission_profile=run.permission_profile, session_id=run.provider_session_id,
            artifacts_dir=run_dir, model=agent.runtime.model or None,
        )
        # The turn boundary: the console groups everything after this under "Turn N" (item 20).
        sink(NormalizedEvent(run_id=run.id, type=EventType.SESSION_RESUMED, source="openagent",
                             data={"session_id": run.provider_session_id, "turn": run.turns,
                                   "prompt": prompt}))
        sink(NormalizedEvent(run_id=run.id, type=EventType.RUN_PHASE, source="openagent",
                             data={"phase": RunPhase.RUNNING.value, "turn": run.turns}))
        try:
            async for event in adapter.resume_run(run.provider_session_id, prompt, request):
                sink(event)
        except Exception as exc:  # noqa: BLE001 - a failed resume is a persisted failure (item 13)
            error_type = getattr(exc, "error_type", None) or _classify_exception(exc)
            turn_state["terminal"] = RunStatus.FAILED
            run.failure_type = error_type
            sink(NormalizedEvent(run_id=run.id, type=EventType.RUN_FAILED, source="openagent",
                                 data={"error_type": error_type, "message": str(exc),
                                       "phase": run.phase, "source": "openagent"}))
        finally:
            self._cli_adapters.pop(run.id, None)
            self.cancellations.discard(run.id)

        event_end = sum(1 for _ in event_log.read())
        self._finalize_resume(run, prompt, turn_state, turn_art, (event_start, event_end))
        return run

    def _finalize_resume(
        self, run: Run, prompt: str, turn_state: dict, turn_art: RunArtifacts,
        event_range: tuple[int, int],
    ) -> None:
        """Rebuild cumulative artifacts from the full event log after a resume turn (spec §32)."""

        # Cumulative artifacts: replay the whole event log so earlier turns' work is preserved even
        # if this turn failed (a failed resume must never erase prior successful artifacts).
        art, _ = self._rebuild_artifacts(run)
        projection = self.projection(run.id)

        wt = WorktreeManager(self.paths.project_root, self.paths.worktrees_dir)
        ws = self._reconstruct_workspace(run)
        art.diff = wt.diff(ws)
        art.files_changed = wt.changed_files(ws)
        run.files_changed = art.files_changed

        if run.id in self._cancelled or turn_state.get("terminal") is RunStatus.CANCELLED:
            final = RunStatus.CANCELLED
        elif turn_state.get("terminal") is None:
            final = RunStatus.FAILED
            run.failure_type = run.failure_type or "no_terminal_event"
        else:
            final = turn_state["terminal"]
        run.status = final
        run.phase = final.value if final.value in _PHASE_VALUES else RunPhase.FAILED.value
        run.completed_at = _now()
        if final is RunStatus.COMPLETED:
            run.failure_type = None  # a successful new turn clears a prior turn's failure (item 18)
        self.repos.runs.upsert(run)

        run_dir = self.paths.run_dir(run.id)
        writer = ArtifactWriter(run_dir)
        # turn_NNN.md is scoped to this turn only; result.json is cumulative for the whole run.
        writer.write_turn(run, prompt, turn_art, event_range)
        writer.write_status(run)
        writer.write_results(run, art, projection)
        writer.write_timeline(run, projection)

    # ------------------------------------------------------------------ replay

    def projection(self, run_id: str) -> RunProjection:
        """Replay ``events.jsonl`` into the current projected state of a run (item 10).

        This is what lets the Run Console be *closed and reopened* — including for a run that is
        still going — and what a restarted app uses to show a live run's history before tailing it.
        """

        projection = RunProjection(run_id)
        for event in EventLog(self.paths.run_dir(run_id)).read():
            projection.apply(event)
        return projection

    def is_live(self, run_id: str) -> bool:
        """Whether this process is currently executing ``run_id`` (so the console can tail it)."""

        return run_id in self._cli_adapters or self.cancellations.get(run_id) is not None

    def resume_support(self, run: Run) -> tuple[bool, str]:
        """Can this run take a follow-up turn right now? If not, why not (item 20)?

        Deliberately honest about mid-turn: a non-interactive CLI process cannot be handed new input
        while it is working, so OpenAgent says "after the current turn completes" instead of
        pretending a message can be injected.
        """

        agent = self.repos.agents.get(run.agent)
        if agent is None:
            return False, "the agent this run used no longer exists"
        rtype = agent.runtime.type
        if rtype not in (RuntimeType.CLI, RuntimeType.CLI.value):
            return False, "follow-up is supported for CLI backends in v0.1"
        if _status_value(run) not in {s.value for s in _TERMINAL}:
            return False, "Follow-up becomes available after the current turn completes."
        if not run.provider_session_id:
            return False, "this backend did not report a session id, so it cannot be resumed"
        return True, ""

    def _rebuild_artifacts(self, run: Run) -> tuple[RunArtifacts, dict]:
        """Fold the full ``events.jsonl`` back into a cumulative :class:`RunArtifacts`."""

        art = RunArtifacts()
        state: dict[str, Any] = {"terminal": None}
        event_log = EventLog(self.paths.run_dir(run.id))
        usage_total: dict[str, Any] = {
            "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
            "reasoning_tokens": 0,
        }
        cost_total: float | None = None
        saw_usage = False
        for event in event_log.read():
            _capture(event, art, run, state)
            etype = event.type if isinstance(event.type, str) else event.type.value
            if etype == EventType.USAGE_UPDATED.value:
                saw_usage = True
                for key in ("input_tokens", "cached_input_tokens", "output_tokens",
                            "reasoning_tokens"):
                    usage_total[key] += int(event.data.get(key) or 0)
                cost = event.data.get("provider_cost")
                if cost is not None:  # cumulative cost across turns (turn1 + turn2 = total, item 12)
                    cost_total = (cost_total or 0.0) + float(cost)
        # Cumulative usage across every turn (item 18) — overrides _capture's last-turn-only value.
        if saw_usage:
            usage_total["provider_cost"] = cost_total
            art.usage = usage_total
        return art, state

    def _reconstruct_workspace(self, run: Run):
        """Rebuild the exact Workspace used for the run so a resumed diff uses the same baseline.

        Restores the persisted copy/in-place flags, source path, and immutable baseline snapshot
        (item 5) — without them a copy/non-git resume would treat every file as newly created.
        """

        from ..workspaces.worktree import Workspace, is_git_repo

        root = Path(run.worktree or run.workspace)
        source = Path(run.source_path) if run.source_path else self.paths.project_root
        baseline_dir = Path(run.baseline_dir) if run.baseline_dir else None
        return Workspace(
            run_id=run.id, root=root, source=source,
            is_git=is_git_repo(root), strategy=run.worktree_strategy,
            branch=run.branch, base_commit=run.base_commit,
            is_copy=run.is_copy, in_place=run.in_place, baseline_dir=baseline_dir,
        )

    async def cancel(self, run_id: str, reason: str = "cancelled by user") -> None:
        """Really stop a run — API and CLI alike (item 9). Idempotent.

        Three paths, in order:

        * **API run in this process** — flip the run's :class:`RunCancellation`. The agent loop sees
          it at its next checkpoint, abandons the provider stream, stops running tools, and returns
          ``cancelled``; the executor then writes the single ``run.cancelled``. (Safe to call from a
          different event loop than the run's — that is the normal case in the TUI.)
        * **CLI run in this process** — kill the process tree; the running executor finalizes.
        * **After a restart** — no live controller, so terminate by PID (identity-verified) and
          finalize the artifacts here.
        """

        run = self.get(run_id)
        if not run:
            return
        if run.status in _TERMINAL or _status_value(run) in {s.value for s in _TERMINAL}:
            return  # idempotent: already finished
        self._cancelled.add(run_id)

        # An in-process run (API or CLI) has a live cancellation flag: raise it first, so an API
        # loop stops even though there is no process to kill.
        signalled = self.cancellations.cancel(run_id, reason)

        adapter = self._cli_adapters.get(run_id)
        if adapter is not None:
            # Same process: kill the tree; the running executor will finalize (emit run.cancelled).
            await adapter.cancel(run_id)
            return
        if signalled:
            return  # the API loop owns finalization — do not race it by writing a status here

        # Cross-process / after restart: terminate by PID with identity verification, then finalize.
        terminate_pid_tree(run.pid, run.pid_started_at)
        run_dir = self.paths.run_dir(run_id)
        EventLog(run_dir, index=self.repos.event_index).append(
            NormalizedEvent(run_id=run_id, type=EventType.RUN_CANCELLED, source="openagent",
                            data={"reason": reason})
        )
        run.status = RunStatus.CANCELLED
        run.phase = RunPhase.CANCELLED.value
        run.completed_at = _now()
        run.failure_type = run.failure_type or "user_cancelled"
        self.repos.runs.upsert(run)
        if run_dir.exists():
            ArtifactWriter(run_dir).write_status(run)

    # ------------------------------------------------------------------ maintenance

    def recover_orphans(self) -> Sequence[str]:
        """Mark active runs this process no longer owns as orphaned (spec §45, item 9.5).

        A run is left running **only** while it is live *in this very process* — its CLI adapter or
        its cancellation controller is still registered here. Anything else is orphaned, and the
        reason is recorded:

        * a missing / dead PID → ``orphaned_pid_gone``;
        * a PID reused by an unrelated process → ``orphaned_pid_reused``;
        * a PID we can't tie to our run → ``orphaned_pid_unknown``;
        * a PID that is *still alive* but which we do not own → ``orphaned_unattached_process``.

        The last case is the item 9.5 correction. A restarted OpenAgent cannot reattach to a previous
        run's stdout/event stream, so continuing to report that run as "running" would be a lie: no
        one is observing it and a follow-up could never be delivered. We fail closed — mark it
        orphaned — and, crucially, **do not kill** the live process; we record its PID and a safe
        summary so the user can decide whether to terminate it (via ``openagent cancel``).
        """

        recovered: list[str] = []
        for run in self.repos.runs.list_active():
            # Owned by *this* process (a live CLI adapter or an in-flight API/CLI cancellation
            # controller) → genuinely still running here; leave it alone.
            if run.id in self._cli_adapters or self.cancellations.get(run.id) is not None:
                continue
            status = run_process_status(run.pid, run.pid_started_at)
            if status == PID_ALIVE:
                run.failure_type = run.failure_type or "orphaned_unattached_process"
                self._record_orphan_event(run, status)
            else:
                run.failure_type = run.failure_type or f"orphaned_pid_{status}"
            run.status = RunStatus.ORPHANED
            run.completed_at = _now()
            self.repos.runs.upsert(run)
            recovered.append(run.id)
        return recovered

    def _record_orphan_event(self, run: Run, status: str) -> None:
        """Write an audit event for an orphaned run whose process is still alive (item 9.5).

        Records the live PID and a safe, actionable summary — and states plainly that the process was
        **not** terminated. Best-effort: a failure to write this note must never crash recovery.
        """

        run_dir = self.paths.run_dir(run.id)
        if not run_dir.exists():
            return
        try:
            EventLog(run_dir, index=self.repos.event_index).append(
                NormalizedEvent(
                    run_id=run.id, type=EventType.LOG, source="openagent",
                    data={
                        "kind": "orphan",
                        "reason": "unattached_live_process",
                        "pid": run.pid,
                        "pid_status": status,
                        "killed": False,
                        "message": (
                            f"pid {run.pid} is still alive but this OpenAgent process cannot "
                            "reattach to its output; marked orphaned and left running. Cancel it "
                            f"explicitly with `openagent cancel --id {run.id}` to stop it."
                        ),
                    },
                )
            )
        except OSError:  # pragma: no cover - the audit note is best-effort
            pass

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


_PHASE_VALUES = {p.value for p in RunPhase}


def _is_redundant_phase(event: NormalizedEvent, run: Run) -> bool:
    """True when a ``run.phase`` event restates the phase the run is already in (item 4)."""

    etype = event.type if isinstance(event.type, str) else event.type.value
    if etype != EventType.RUN_PHASE.value:
        return False
    return str(event.data.get("phase") or "") == run.phase


def _terminal_event_type(status: RunStatus) -> EventType:
    return {
        RunStatus.COMPLETED: EventType.RUN_COMPLETED,
        RunStatus.CANCELLED: EventType.RUN_CANCELLED,
    }.get(status, EventType.RUN_FAILED)


_TERMINAL_EVENT_TYPES = frozenset({
    EventType.RUN_COMPLETED.value, EventType.RUN_FAILED.value, EventType.RUN_CANCELLED.value,
})


def _is_terminal_event(event: NormalizedEvent) -> bool:
    etype = event.type if isinstance(event.type, str) else event.type.value
    return etype in _TERMINAL_EVENT_TYPES


def _status_of_terminal(event: NormalizedEvent) -> RunStatus | None:
    etype = event.type if isinstance(event.type, str) else event.type.value
    return {
        EventType.RUN_COMPLETED.value: RunStatus.COMPLETED,
        EventType.RUN_FAILED.value: RunStatus.FAILED,
        EventType.RUN_CANCELLED.value: RunStatus.CANCELLED,
    }.get(etype)


def _typed(exc: Exception, error_type: str) -> Exception:
    """Tag an exception with a normalized failure type so ``_fail`` records the right one."""

    exc.error_type = error_type  # type: ignore[attr-defined]
    return exc


def _classify_exception(exc: Exception) -> str:
    """Map an unexpected exception onto OpenAgent's normalized failure vocabulary (item 13)."""

    if isinstance(exc, RunCancelled):
        return "user_cancelled"
    if isinstance(exc, FileNotFoundError):
        return "cli_not_found"
    if isinstance(exc, PermissionError):
        return "process_start_failed"
    if isinstance(exc, OSError):
        return "process_start_failed"
    text = str(exc).lower()
    if "provider" in text and "not found" in text:
        return "provider_not_found"
    if "credential" in text or "keyring" in text:
        return "credential_missing"
    return "unknown"


def _lower_safety_warning(workspace) -> str:
    if workspace.in_place:
        return "Ran in place (worktree 'none'): edits were applied directly to your project."
    return "Ran in a non-git copy (lower safety): changes are not versioned."


def _capture(event: NormalizedEvent, art: RunArtifacts | None, run: Run, state: dict) -> None:
    etype = event.type if isinstance(event.type, str) else event.type.value
    data = event.data
    # The PID now arrives on process.started — run.started is OpenAgent's own event and has no pid.
    if etype == EventType.PROCESS_STARTED.value and data.get("pid"):
        run.pid = data["pid"]
        run.pid_started_at = data.get("create_time") or pid_identity(data["pid"])
    elif etype == EventType.RUN_PHASE.value and data.get("phase"):
        run.phase = str(data["phase"])
    elif etype == EventType.SESSION_CREATED.value and data.get("provider_session_id"):
        run.provider_session_id = data["provider_session_id"]
    elif etype == EventType.MESSAGE_COMPLETED.value and data.get("text"):
        if art is not None:
            art.summary = data["text"]
    elif etype == EventType.TEST_COMPLETED.value:
        if art is not None:
            art.tests = TestSummary(
                ran=True, passed=data.get("passed"), exit_code=data.get("exit_code"),
                command=data.get("command", ""),
            )
    elif etype in (EventType.FILE_CREATED.value, EventType.FILE_MODIFIED.value,
                   EventType.FILE_DELETED.value):
        if art is not None:
            verb = etype.split(".")[1]
            art.changes.append(f"{verb} {data.get('path', '')}".strip())
    elif etype in (EventType.COMMAND_COMPLETED.value, EventType.LOG.value):
        if art is not None:
            if data.get("stderr"):
                art.log_lines.append(str(data["stderr"]))
            if data.get("command"):
                art.log_lines.append(f"$ {data['command']} -> exit {data.get('exit_code')}")
    elif etype == EventType.USAGE_UPDATED.value:
        if art is not None:
            art.usage = {k: data.get(k) for k in
                         ("input_tokens", "cached_input_tokens", "output_tokens", "provider_cost")}
    elif etype == EventType.RUN_COMPLETED.value:
        state["terminal"] = RunStatus.COMPLETED
        state["emitted_terminal"] = True
    elif etype == EventType.RUN_CANCELLED.value:
        state["terminal"] = RunStatus.CANCELLED
        state["emitted_terminal"] = True
    elif etype == EventType.RUN_FAILED.value:
        state["terminal"] = RunStatus.FAILED
        state["emitted_terminal"] = True
        run.failure_type = data.get("error_type") or run.failure_type or "unknown"
        if art is not None and not art.error:
            art.error = {
                "error_type": run.failure_type,
                "message": data.get("message") or "",
                "phase": data.get("phase") or run.phase,
                "source": data.get("source") or event.source,
            }
