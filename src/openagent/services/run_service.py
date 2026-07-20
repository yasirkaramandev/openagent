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

import asyncio
import contextlib
import os
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.cancellation import CancellationRegistry, RunCancellation, RunCancelled
from ..core.events import EventType, NormalizedEvent, RunPhase
from ..core.models import AgentProfile, ProcessIdentity, Run, RunStatus, RuntimeType, enum_value
from ..core.permissions import get_profile
from ..core.projection import RunProjection
from ..credentials.redaction import acquire_scoped_secret, release_secret_scope
from ..reporting.artifacts import ArtifactWriter, RunArtifacts, TestSummary
from ..runtimes.api_agent.loop import run_api_agent
from ..runtimes.cli.base import CliAdapter, CliRunRequest
from ..runtimes.cli.cli_auth import ChildEnvironmentPlan, build_child_environment
from ..runtimes.cli.registry import build_cli_adapter
from ..security.approvals import ApprovalCallback, ApprovalGate
from ..security.execution_backend import (
    CONTAINER_SANDBOX,
    EXECUTION_BACKENDS,
    ExecutionBackendError,
    build_execution_backend,
)
from ..security.process import (
    PID_ALIVE,
    PID_GONE,
    PID_REUSED,
    TerminationOutcome,
    TerminationResult,
    capture_process_identity,
    process_identity_status,
    run_process_status,
    terminate_pid_tree,
)
from ..storage.event_log import EventExportError, EventLog
from ..storage.projects import canonical_root
from ..tools.base import AskUserResolver, ToolContext
from ..tools.registry import ToolExecutor
from ..workspaces.worktree import NONE, STRATEGIES, WorktreeManager
from .preflight import PreflightReport, PreflightService

if TYPE_CHECKING:
    from ..app import OpenAgentApp

EventHook = Callable[[NormalizedEvent], None]

_ACTIVE = {RunStatus.QUEUED, RunStatus.STARTING, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL}
_TERMINAL = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.ORPHANED}

#: Statuses a follow-up turn may continue from (§5). Deliberately NOT "everything terminal":
#: ``orphaned`` and ``cancelled`` are terminal but must never be resumed — see ``_validate_resume``.
_RESUMABLE_STATUSES = {RunStatus.COMPLETED.value, RunStatus.FAILED.value}

#: Why each orphan reason refuses a resume. Every one of them refuses; only the wording differs, so
#: the user is told what is actually true about their run rather than a generic "no".
_ORPHAN_RESUME_REFUSAL = {
    "orphaned_unattached_process": (
        "this run is orphaned and its backend process may still be RUNNING but unowned; resuming "
        "would start a second process against the same session. Stop it first with "
        "`openagent cancel --id <run-id>`, then start a new run"
    ),
    "orphaned_pid_reused": (
        "this run is orphaned and its recorded PID now belongs to an unrelated process, so its "
        "session cannot be reasoned about — start a new run"
    ),
    "orphaned_pid_unknown": (
        "this run is orphaned and its process identity cannot be verified — start a new run"
    ),
    "orphaned_pid_gone": (
        "this run is orphaned and its process is gone; OpenAgent cannot safely re-attach to that "
        "session — start a new run"
    ),
}
_ORPHAN_RESUME_DEFAULT = (
    "this run is orphaned; OpenAgent lost ownership of it and cannot safely resume it — "
    "start a new run"
)


class CancelOutcome(str, Enum):
    """The concrete result of a cancel request, so a caller never reports a false success (§3.3).

    ``openagent cancel`` used to print "cancelled" unconditionally — even when the run had already
    finished or nothing could be stopped. The service now returns exactly what happened and the CLI
    prints (and exits) accordingly.
    """

    SIGNALLED = "signalled"  # an in-process loop/adapter was told to stop; it finalizes itself
    TERMINATED = "terminated"  # a live process tree was identity-verified and killed
    ALREADY_TERMINAL = "already_terminal"  # the run had already reached a terminal state
    NOT_FOUND = "not_found"  # no such run
    WRONG_PROJECT = "wrong_project"
    ALREADY_GONE = "already_gone"
    IDENTITY_UNKNOWN = "identity_unknown"
    IDENTITY_MISMATCH = "identity_mismatch"
    ACCESS_DENIED = "access_denied"
    TERMINATION_FAILED = "termination_failed"
    SURVIVORS_REMAINING = "survivors_remaining"
    #: An orphaned run with no safely identifiable live process (e.g. orphaned_pid_gone/reused).
    NOT_CANCELLABLE = "not_cancellable"


_TERMINATION_CANCEL_OUTCOME = {
    TerminationOutcome.ALREADY_GONE: CancelOutcome.ALREADY_GONE,
    TerminationOutcome.IDENTITY_UNKNOWN: CancelOutcome.IDENTITY_UNKNOWN,
    TerminationOutcome.IDENTITY_MISMATCH: CancelOutcome.IDENTITY_MISMATCH,
    TerminationOutcome.ACCESS_DENIED: CancelOutcome.ACCESS_DENIED,
    TerminationOutcome.TERMINATION_FAILED: CancelOutcome.TERMINATION_FAILED,
    TerminationOutcome.SURVIVORS_REMAINING: CancelOutcome.SURVIVORS_REMAINING,
}


def _cancel_outcome(result: TerminationResult) -> CancelOutcome:
    """Translate process evidence without collapsing distinct failure modes."""

    if result.verified_terminated:
        return CancelOutcome.TERMINATED
    return _TERMINATION_CANCEL_OUTCOME.get(result.outcome, CancelOutcome.TERMINATION_FAILED)


class RunError(RuntimeError):
    pass


class _ConcurrentRunUpdate(RunError):
    """Another writer won the run revision; this worker must stop without terminal side effects."""


class TerminalEventAppendError(RunError):
    """The authoritative terminal event could not be appended."""


class TerminalEventExportError(RunError):
    """The event committed to SQLite but its JSONL projection could not be exported."""


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
        #: One lock per run so a resume/follow-up turn cannot start while another is running for the
        #: same run (§4.1). A second concurrent follow-up is rejected outright rather than silently
        #: overwriting ``_cli_adapters[run.id]`` and the cancellation controller of the first.
        self._resume_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ CRUD

    def create(
        self,
        *,
        agent_name: str,
        prompt: str,
        worktree: str = "auto",
        permission_profile: str | None = None,
        confirm_in_place: bool = False,
        execution_backend: str = "host-restricted",
        container_runtime: str | None = None,
        container_image: str | None = None,
        commit_agent_changes: bool = False,
    ) -> Run:
        agent = self.repos.agents.get(agent_name)
        if not agent:
            raise RunError(f"agent {agent_name!r} not found")
        profile = permission_profile or agent.permission_profile
        prof = get_profile(profile)  # validate
        if worktree not in STRATEGIES:
            raise RunError(f"unknown worktree strategy {worktree!r}; choose from {STRATEGIES}")
        if execution_backend not in EXECUTION_BACKENDS:
            raise RunError(
                f"unknown execution backend {execution_backend!r}; choose from {EXECUTION_BACKENDS}"
            )
        if execution_backend == CONTAINER_SANDBOX:
            if worktree == NONE:
                raise RunError("container-sandbox refuses worktree 'none'")
            if not container_image:
                raise RunError("container-sandbox requires --container-image with a local image")
            if agent.runtime.type in (RuntimeType.CLI, RuntimeType.CLI.value):
                # CLI adapters own a long-lived streaming process and cannot yet be launched through
                # the one-shot structured command backend. Ignoring the selection and running on the
                # host would be a security boundary violation, so this combination fails closed.
                raise RunError(
                    "container-sandbox currently supports API-agent tool commands only; "
                    "CLI runs are refused rather than falling back to the host"
                )
        if worktree == NONE and prof.can_edit_files and not confirm_in_place:
            raise RunError(
                "worktree 'none' runs a file-editing agent directly in your project with no "
                "isolation; pass explicit confirmation to proceed"
            )
        run_id = _new_run_id()
        # Record which project this run belongs to, and where its artifacts will live (spec §3). The
        # DB is global, so without this the run cannot be scoped and reads have to guess the artifact
        # location from the current working directory.
        root = canonical_root(self.paths.project_root)
        run = Run(
            id=run_id,
            agent=agent_name,
            prompt=prompt,
            workspace=str(self.paths.project_root),
            permission_profile=profile,
            worktree_strategy=worktree,
            project_id=self.project_id,
            project_root=str(root),
            project_state_dir=str(self.paths.project_state_dir),
            artifact_dir=str(self.paths.run_dir(run_id)),
            execution_backend=execution_backend,
            container_runtime=container_runtime,
            container_image=container_image,
            commit_agent_changes=commit_agent_changes,
        )
        self.repos.runs.create_run(run)
        return run

    def _save_progress(self, run: Run) -> None:
        if not self.repos.runs.update_progress(run):
            raise _ConcurrentRunUpdate(f"run {run.id} changed concurrently")

    def _save_transition(self, run: Run, previous: RunStatus | str) -> None:
        if not self.repos.runs.transition_run(run, expected_statuses={previous}):
            raise _ConcurrentRunUpdate(f"run {run.id} lifecycle changed concurrently")

    # ------------------------------------------------------------------ project scope (§3)

    @property
    def project_id(self) -> str:
        """The project this service instance is bound to."""

        return self.app.project.id

    def run_dir_for(self, run: Run) -> Path:
        """Where ``run``'s artifacts live — from the run itself, never from the current project.

        A run created by another project records its own ``artifact_dir``; resolving through the
        ambient ``Paths`` would look under *this* project and find nothing (spec §3.7). Runs written
        before v0.1.3 have no ``artifact_dir``, so they fall back to the old behaviour.
        """

        if run.artifact_dir:
            return Path(run.artifact_dir)
        return self.paths.run_dir(run.id)

    def _run_dir_by_id(self, run_id: str, *, all_projects: bool = False) -> Path:
        run = self._require_run(run_id, all_projects=all_projects)
        return self.run_dir_for(run) if run else self.paths.run_dir(run_id)

    def get(self, run_id: str, *, all_projects: bool = False) -> Run | None:
        run = self.repos.runs.get(run_id)
        if (
            run is not None
            and not all_projects
            and run.project_id is not None
            and run.project_id != self.project_id
        ):
            return None
        return run

    def _require_run(self, run_id: str, *, all_projects: bool = False) -> Run:
        run = self.repos.runs.get(run_id)
        if run is None:
            raise RunError(f"run {run_id!r} not found")
        if not all_projects and run.project_id is not None and run.project_id != self.project_id:
            raise RunError(
                f"run {run_id!r} belongs to another project; pass explicit all_projects authority"
            )
        return run

    def list(self, limit: int = 50, *, all_projects: bool = False) -> Sequence[Run]:
        """This project's recent runs. Pass ``all_projects`` for the explicit global view (§3.5)."""

        return self.repos.runs.list(limit, project_id=self.project_id, all_projects=all_projects)

    # ------------------------------------------------------------------ execution

    async def execute(
        self,
        run: Run,
        on_event: EventHook | None = None,
        approval_callback: ApprovalCallback | None = None,
        ask_user_callback: AskUserResolver | None = None,
    ) -> Run:
        agent = self.repos.agents.get(run.agent)
        if not agent:
            raise RunError(f"agent {run.agent!r} not found")

        cancel = self.cancellations.create(run.id)
        cancel.bind()

        run_dir = self.run_dir_for(run)
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
                sink(
                    NormalizedEvent(
                        run_id=run.id,
                        type=EventType.RUN_PHASE,
                        source="openagent",
                        data={"phase": new.value, **data},
                    )
                )
                self._save_progress(run)

            # The one and only run.started for this run — OpenAgent's, never a backend's (item 4).
            previous_status = run.status
            run.status = RunStatus.STARTING
            self._save_transition(run, previous_status)
            sink(
                NormalizedEvent(
                    run_id=run.id,
                    type=EventType.RUN_STARTED,
                    source="openagent",
                    data={
                        "agent": run.agent,
                        "workspace": run.workspace,
                        "permission_profile": run.permission_profile,
                        "worktree_strategy": run.worktree_strategy,
                    },
                )
            )

            workspace = None
            wt = WorktreeManager(self.paths.project_root, self.paths.worktrees_dir)
            try:
                # ---- preflight: prove the agent can run before creating anything (item 7) --------
                phase(RunPhase.PREFLIGHT)
                report = await self.preflight.check(
                    agent_name=run.agent,
                    permission_profile=run.permission_profile,
                    run_id=run.id,
                )
                sink(
                    NormalizedEvent(
                        run_id=run.id,
                        type=EventType.LOG,
                        source="openagent",
                        data={
                            "kind": "preflight",
                            "ok": report.ok,
                            "checks": [c.line() for c in report.checks],
                        },
                    )
                )
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
                self._save_progress(run)
                if workspace.lower_safety:
                    art.warnings.append(_lower_safety_warning(workspace))
                sink(
                    NormalizedEvent(
                        run_id=run.id,
                        type=EventType.WORKSPACE_PREPARED,
                        source="openagent",
                        data={
                            "workspace": str(workspace.root),
                            "strategy": workspace.strategy,
                            "branch": workspace.branch,
                            "in_place": workspace.in_place,
                        },
                    )
                )
                cancel.check()

                # ---- backend ----------------------------------------------------------------
                phase(RunPhase.STARTING_BACKEND)
                previous_status = run.status
                run.status = RunStatus.RUNNING
                self._save_transition(run, previous_status)
                phase(RunPhase.RUNNING)

                rtype = agent.runtime.type
                if rtype is RuntimeType.API_AGENT or rtype == RuntimeType.API_AGENT.value:
                    await self._run_api(
                        run,
                        agent,
                        workspace.root,
                        sink,
                        art,
                        state,
                        approval_callback,
                        workspace.describe_for_agent(),
                        ask_user_callback,
                        cancel,
                    )
                else:
                    await self._run_cli(run, agent, workspace.root, sink, state, run_dir)
            except RunCancelled as exc:
                state["terminal"] = RunStatus.CANCELLED
                self._cancelled.add(run.id)
                run.failure_type = "user_cancelled"
                if state.get("emitted_terminal") is not True:
                    sink(
                        NormalizedEvent(
                            run_id=run.id,
                            type=EventType.RUN_CANCELLED,
                            source="openagent",
                            data={"reason": exc.reason, "phase": run.phase},
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - every runtime error becomes a persisted failure
                self._fail(run, sink, state, exc)
            finally:
                self.cancellations.discard(run.id)

            # ---- finalize (items 1 + 9.4) -------------------------------------------------
            # The ``finalizing`` phase and the diff happen BEFORE the single terminal event, which is
            # written LAST — so events[-1] is always the terminal event and the projection never
            # settles on "status: completed / phase: finalizing".
            sink(
                NormalizedEvent(
                    run_id=run.id,
                    type=EventType.RUN_PHASE,
                    source="openagent",
                    data={"phase": RunPhase.FINALIZING.value},
                )
            )
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
                            art.error = {
                                "error_type": "finalization_failed",
                                "message": str(exc) or exc.__class__.__name__,
                                "phase": RunPhase.FINALIZING.value,
                                "source": "openagent",
                            }
                if run.commit_agent_changes and state.get("terminal") is RunStatus.COMPLETED:
                    try:
                        run.agent_commit_sha = wt.commit_all(
                            workspace,
                            _agent_commit_message(agent),
                        )
                    except Exception as exc:  # noqa: BLE001 - requested commit is a release gate
                        state["terminal"] = RunStatus.FAILED
                        run.failure_type = "agent_commit_failed"
                        state["pending_terminal"] = None
                        art.error = {
                            "error_type": "agent_commit_failed",
                            "message": str(exc),
                            "phase": RunPhase.FINALIZING.value,
                            "source": "openagent",
                        }

            final = self._resolve_final(run, state)
            previous_status = run.status
            run.status = final
            run.phase = final.value if final.value in _PHASE_VALUES else RunPhase.FAILED.value
            run.completed_at = _now()
            if final is RunStatus.COMPLETED:
                run.failure_type = None  # a completed run carries no failure type (item 18)
                if not art.summary:
                    art.summary = projection.final_message or "Run completed."
            self._save_transition(run, previous_status)

            # Reuse the backend's own terminal event when it still matches the resolved outcome
            # (keeps its richer data); otherwise synthesize one for the reconciled status.
            pending = state.get("pending_terminal")
            if pending is not None and _status_of_terminal(pending) is final:
                terminal_event = pending
            else:
                terminal_event = NormalizedEvent(
                    run_id=run.id,
                    type=_terminal_event_type(final),
                    source="openagent",
                    data={"status": final.value}
                    if final is RunStatus.COMPLETED
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
            writer.write_expected(run, art)
            writer.write_auxiliary(art)
            writer.write_timeline(run, projection)
            writer.write_integrity(run)
            self._save_progress(run)
            try:
                saved = event_log.append(terminal_event)
            except EventExportError:
                # SQLite already contains the terminal event. Keep the real outcome and explicitly
                # mark the file bundle as partial/repairable instead of inventing a contradictory
                # run.failed solely because the non-authoritative JSONL projection is stale.
                export_failure = {
                    "stage": "event_export",
                    "message": "terminal event committed; JSONL export requires repair",
                }
                art.warnings.append(
                    "events.jsonl export failed; SQLite event store is authoritative"
                )
                writer.write_status(run, artifacts_partial=True, artifact_failure=export_failure)
                writer.write_results(
                    run,
                    art,
                    projection,
                    artifacts_partial=True,
                    artifact_failure=export_failure,
                )
                writer.write_integrity(run)
                self._save_progress(run)
                state["terminal_written"] = True
                return run
            except Exception as exc:  # noqa: BLE001 - classified separately from artifact writes
                run.failure_type = "terminal_event_append_failed"
                raise TerminalEventAppendError(
                    "authoritative terminal event append failed"
                ) from exc
            state["terminal_written"] = True
            if on_event is not None:
                with contextlib.suppress(Exception):  # a UI-notify failure must not fail the run
                    on_event(saved)
            return run
        except _ConcurrentRunUpdate:
            return self.repos.runs.get(run.id) or run
        except Exception as exc:  # noqa: BLE001 - the outer lifecycle boundary (item 9.4)
            return self._finalize_failure(run, run_dir, art, state, exc)
        finally:
            self.cancellations.discard(run.id)
            self._cancelled.discard(run.id)

    def _finalize_failure(
        self, run: Run, run_dir: Path, art: RunArtifacts, state: dict, exc: Exception
    ) -> Run:
        """Force a terminal state after a setup/finalize error (item 9.4), best-effort throughout.

        A run never stays running, and an artifact-write failure never looks like success. The first
        real error wins (item 5): a run already resolved to failed/cancelled keeps that terminal
        type; one heading for *completed* (or unresolved) is flipped to FAILED, because its result
        could not be finished.
        """

        prior = state.get("terminal")
        final = prior if prior in (RunStatus.CANCELLED, RunStatus.FAILED) else RunStatus.FAILED
        previous_status = run.status
        if final is RunStatus.FAILED:
            run.failure_type = run.failure_type or "artifact_write_failed"
            if not art.error:
                art.error = {
                    "error_type": run.failure_type,
                    "message": str(exc) or exc.__class__.__name__,
                    "phase": run.phase,
                    "source": "openagent",
                }
        run.status = final
        run.phase = final.value if final.value in _PHASE_VALUES else RunPhase.FAILED.value
        run.completed_at = _now()
        # The DB is the source of truth. If a completed outcome was already reserved before a
        # mandatory artifact failed, correct that reservation to failed; otherwise use the ordinary
        # revision-aware transition. Losing either CAS means another actor owns finalization and this
        # worker must not append an event or touch its bundle.
        if previous_status is RunStatus.COMPLETED and final is RunStatus.FAILED:
            accepted = self.repos.runs.mark_artifact_failure(
                run,
                expected_status=previous_status,
            )
        else:
            accepted = self.repos.runs.transition_run(
                run,
                expected_statuses={previous_status},
            )
        if not accepted:
            return self.repos.runs.get(run.id) or run
        self._recover_artifacts(
            run,
            run_dir,
            exc,
            stage="finalize",
            wrote_terminal=state.get("terminal_written", False),
            art=art,
        )
        return run

    def _recover_artifacts(
        self,
        run: Run,
        run_dir: Path,
        exc: Exception,
        *,
        stage: str,
        wrote_terminal: bool,
        art: RunArtifacts | None = None,
        prompt: str | None = None,
    ) -> None:
        """Make the WHOLE artifact bundle consistent with a failed/cancelled run (§5), best-effort.

        The old recovery only rewrote ``status.json`` + ``result.json`` and left ``timeline.md`` (and
        ``output.md``/``handoff.md``/``tests.json``/``changes.diff``/``logs.txt``) claiming the run
        had *completed*. Now every artifact is regenerated from the reconciled run + a fresh
        projection replay, so none can still say "completed", and the bundle is stamped
        ``artifacts_partial`` with the failing ``stage`` so a reader knows it was recovered. Each step
        is individually suppressed — recording the failure must never itself raise, and one failing
        write must not abort the rest.
        """

        final = run.status
        is_cancelled = enum_value(final) == RunStatus.CANCELLED.value
        art_failure = {"stage": stage, "message": str(exc) or exc.__class__.__name__}

        # Rebuild cumulative artifacts + projection from whatever is on disk (best-effort). A live
        # ``art`` from execute() is preferred (it already carries this run's diff/summary/error).
        bundle = art
        if bundle is None:
            try:
                bundle, _ = self._rebuild_artifacts(run)
            except Exception:  # noqa: BLE001 - the log may itself be unreadable
                bundle = RunArtifacts()
        if not bundle.error and not is_cancelled:
            bundle.error = {
                "error_type": run.failure_type or "artifact_write_failed",
                "message": art_failure["message"],
                "phase": run.phase,
                "source": "openagent",
            }
        try:
            projection: RunProjection | None = self.projection(run.id)
        except Exception:  # noqa: BLE001
            projection = None

        with contextlib.suppress(Exception):
            writer = ArtifactWriter(run_dir)
            steps: list[Callable[[], Any]] = [
                lambda: writer.write_status(
                    run, artifacts_partial=True, artifact_failure=art_failure
                ),
                lambda: writer.write_results(
                    run, bundle, projection, artifacts_partial=True, artifact_failure=art_failure
                ),
            ]
            if projection is not None:
                steps.append(lambda: writer.write_timeline(run, projection))
            if prompt is not None:
                steps.append(lambda: writer.write_turn(run, prompt, bundle, None))
            steps.append(lambda: writer.write_expected(run, bundle))
            steps.append(lambda: writer.write_auxiliary(bundle))
            for step in steps:
                with contextlib.suppress(Exception):
                    step()
            with contextlib.suppress(Exception):
                writer.write_integrity(run)

        # Terminal event last, after every artifact attempt. A failed/cancelled outcome may still be
        # recorded when a mandatory artifact is impossible, but a provisional success never reaches
        # this recovery path: it was corrected to failed by the outer lifecycle boundary.
        if not wrote_terminal:
            with contextlib.suppress(Exception):
                EventLog(run_dir, index=self.repos.event_index).append(
                    NormalizedEvent(
                        run_id=run.id,
                        type=_terminal_event_type(final),
                        source="openagent",
                        data={"status": enum_value(final), "error_type": run.failure_type},
                    )
                )

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
        sink(
            NormalizedEvent(
                run_id=run.id,
                type=EventType.RUN_FAILED,
                source="openagent",
                data={
                    "error_type": error_type,
                    "message": message,
                    "phase": run.phase,
                    "source": "openagent",
                },
            )
        )

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
        if etype in (
            EventType.RUN_STARTED.value,
            EventType.PROCESS_STARTED.value,
            EventType.SESSION_CREATED.value,
        ):
            self._save_progress(run)

    async def _run_api(
        self,
        run,
        agent,
        root: Path,
        sink,
        art,
        state,
        approval_callback: ApprovalCallback | None = None,
        workspace_note: str = "",
        ask_user_callback: AskUserResolver | None = None,
        cancel: RunCancellation | None = None,
    ) -> None:
        provider = self.repos.providers.get_by_name(agent.runtime.provider or "")
        if not provider:
            raise RunError(f"provider {agent.runtime.provider!r} not found")
        api_key = self.app.credentials.resolve(provider.credential)
        acquire_scoped_secret(run.id, api_key)
        try:
            # Go through the service seam so tests and alternate provider factories can replace the
            # adapter without bypassing the run-scoped secret lifetime above. ``adapter_for`` is
            # intentionally side-effect free; this scope owns registration and release.
            adapter = self.app.providers.adapter_for(provider)
        except BaseException:
            release_secret_scope(run.id)
            raise
        transport = getattr(adapter, "transport", None)
        if transport is not None:
            transport.cancellation = cancel
        profile = get_profile(run.permission_profile)

        def tool_emit(name: str, data: dict) -> None:
            try:
                etype = EventType(name)
            except ValueError:
                etype = EventType.LOG
            sink(NormalizedEvent(run_id=run.id, type=etype, source="api-agent", data=data))

        try:
            execution_backend = build_execution_backend(
                run.execution_backend,
                workspace=root,
                container_image=run.container_image,
                container_runtime=run.container_runtime,
                worktree_strategy=run.worktree_strategy,
            )
            execution_backend.validate()
        except ExecutionBackendError as exc:
            transport = getattr(adapter, "transport", None)
            if transport is not None:
                await transport.aclose()
            release_secret_scope(run.id)
            raise _typed(exc, "execution_backend_unavailable") from exc
        ctx = ToolContext(
            workspace_root=root,
            profile=profile,
            approval_gate=ApprovalGate(
                auto_approve=not profile.require_approval_for_destructive,
                callback=approval_callback,
                emit=tool_emit,
                run_id=run.id,
            ),
            run_id=run.id,
            emit=tool_emit,
            ask_user_callback=ask_user_callback,
            # A blocking command (run_command/run_tests) polls this so a Cancel kills its process
            # tree mid-run instead of waiting for it to finish (item 9.2).
            cancellation=cancel,
            execution_backend=execution_backend,
        )
        executor = ToolExecutor(ctx)
        try:
            outcome = await run_api_agent(
                run_id=run.id,
                agent=agent,
                prompt=run.prompt,
                adapter=adapter,
                executor=executor,
                workspace_root=root,
                emit=sink,
                workspace_note=workspace_note,
                cancellation=cancel,
            )
        finally:
            # Always release the HTTP transport — on success, failure, and cancellation alike.
            transport = getattr(adapter, "transport", None)
            if transport is not None:
                await transport.aclose()
            release_secret_scope(run.id)

        art.summary = outcome.summary or art.summary
        art.usage = outcome.usage.model_dump()
        if outcome.cancelled:
            raise RunCancelled(outcome.error_message or "cancelled by user")
        if outcome.completed:
            state["terminal"] = RunStatus.COMPLETED
        else:
            state["terminal"] = RunStatus.FAILED
            run.failure_type = outcome.error_type or "unknown"
            sink(
                NormalizedEvent(
                    run_id=run.id,
                    type=EventType.RUN_FAILED,
                    source="api-agent",
                    data={
                        "error_type": run.failure_type,
                        "message": outcome.error_message or "the API agent did not complete",
                        "phase": run.phase,
                        "source": "api-agent",
                    },
                )
            )

    def _cli_credential_plan(self, cli_type: str, run_id: str) -> ChildEnvironmentPlan:
        """Resolve the credentials a CLI child needs and register them for redaction.

        ``CliRunRequest.credential_env`` existed since 0.1.x and every adapter forwarded it to
        ``minimal_environment()``, but nothing ever filled it in — CLI children were started with
        no credentials whatsoever. Runs only worked because the CLI re-read its own login file, so
        an ``ANTHROPIC_API_KEY``-based setup silently had no way to succeed.

        Resolution happens per turn rather than being stored on the run: a rotated key must take
        effect on resume, and a run record that carried the value would be a secret at rest.

        The values are registered under the run's redaction scope *before* the child starts, so
        anything the CLI echoes back — an error message quoting the key, a debug line — is redacted
        on the way into the event log. The scope is released in the caller's ``finally``.
        """

        plan = build_child_environment(cli_type)
        for secret in plan.secret_values():
            acquire_scoped_secret(run_id, secret)
        return plan

    async def _run_cli(
        self, run, agent: AgentProfile, root: Path, sink, state, run_dir: Path
    ) -> None:
        cli_type = agent.runtime.cli or ""
        adapter = build_cli_adapter(cli_type)
        self._cli_adapters[run.id] = adapter
        plan = self._cli_credential_plan(cli_type, run.id)
        request = CliRunRequest(
            run_id=run.id,
            prompt=run.prompt,
            workspace=root,
            permission_profile=run.permission_profile,
            credential_env=plan.as_child_env(),
            # Scratch files a CLI needs (Codex's --output-last-message) belong to OpenAgent, not to
            # the user's project — keep them out of the workspace and out of the diff (item 6).
            artifacts_dir=run_dir,
            model=agent.runtime.model or None,
            reasoning_effort=agent.runtime.reasoning_effort,
        )
        try:
            async for event in adapter.start_run(request):
                sink(event)
        finally:
            self._cli_adapters.pop(run.id, None)
            release_secret_scope(run.id)

    # ------------------------------------------------------------------ resume / cancel

    async def resume(
        self,
        run_id: str,
        prompt: str,
        on_event: EventHook | None = None,
        *,
        all_projects: bool = False,
    ) -> Run:
        """Take a follow-up turn on a CLI run under the **same** lifecycle contract as the first run.

        Item 9.4/§4: a resume turn is not a second-class path. Exactly one resume runs per run at a
        time (§4.1); the turn's terminal event is buffered and written **last** (§4.2); every step —
        adapter build, backend stream, diff, all artifact writes, DB persistence — lives inside one
        exception boundary so a failure anywhere yields a terminal failed/cancelled turn, never a
        run left ``running`` and never a success reported over a failed artifact write (§4.3/§4.4).
        """

        run = self._require_run(run_id, all_projects=all_projects)
        agent = self.repos.agents.get(run.agent)

        # §4.1 Per-run lock, checked first because it is the most specific diagnosis of "a turn is in
        # flight *right now*" — and because it closes the window between this call and
        # _resume_locked() flipping the status to running, which the status check below cannot see.
        # asyncio is single threaded and there is no await between the check and acquire, so this
        # cannot race.
        lock = self._resume_locks.setdefault(run.id, asyncio.Lock())
        if lock.locked():
            raise RunError("a turn is already running for this run")

        # A prior process may have committed the durable claim and died before its first ordinary
        # domain update. The JSON payload now mirrors that claim, so inspect the lease before the
        # generic "running is not resumable" policy. Conclusive owner death terminalizes the run as
        # orphaned; a live or unverifiable owner remains protected and is never reattached/stolen.
        if run.active_turn_id is not None:
            recovered = self._reclaim_if_owner_is_dead(run.id)
            if recovered:
                raise RunError(
                    "the previous turn owner died; this run was marked orphaned and its CLI "
                    "session was not reattached. Start a new run for safe recovery"
                )
            raise RunError("a turn is already running for this run")

        # The service enforces the resume policy itself (§5, §22). This used to live only in
        # resume_support(), which just the TUI called — so any direct call (CLI, tests, another
        # screen) walked past every guard, including the one that stops a second process being
        # attached to a live orphan's session.
        ok, why = self._validate_resume(run)
        if not ok:
            raise RunError(why)
        if not agent or agent.runtime.type not in (RuntimeType.CLI, RuntimeType.CLI.value):
            raise RunError("resume is currently supported for CLI agents only")
        if not run.provider_session_id:
            raise RunError("no session id recorded for this run")

        # §8 The process-local lock above is a fast, well-worded rejection for the common case, but
        # it is invisible to a second *process* — and OpenAgent is explicitly multi-process (a TUI in
        # one terminal, a CLI in another, one global database). The durable claim is a single
        # conditional UPDATE, so the database, not this process, decides who owns the turn.
        turn_id = f"turn_{uuid.uuid4().hex[:16]}"
        identity = capture_process_identity(os.getpid())
        claimed = self.repos.runs.claim_turn(
            run.id,
            turn_id=turn_id,
            pid=os.getpid(),
            create_time=identity.create_time if identity else 0.0,
            started_at=_now().isoformat(),
        )
        if not claimed:
            recovered = self._reclaim_if_owner_is_dead(run.id)
            if recovered:
                raise RunError(
                    "the previous turn owner died; this run was marked orphaned and its CLI "
                    "session was not reattached. Start a new run for safe recovery"
                )
        if not claimed:
            raise RunError("a turn is already running for this run")

        result: Run | None = None
        try:
            async with lock:
                # Re-read: the claim moved the row to running and bumped its revision, so the
                # in-memory copy from before the claim is already stale.
                current = self.repos.runs.get(run.id) or run
                result = await self._resume_locked(current, agent, prompt, on_event)
        finally:
            with contextlib.suppress(Exception):
                self.repos.runs.release_turn(run.id, turn_id=turn_id)
            # Locks are per in-flight turn, not permanent run state. Keeping one for every run made
            # a long-lived TUI/service grow without bound and retained stale event-loop objects.
            if not lock.locked():
                self._resume_locks.pop(run.id, None)
        return self.repos.runs.get(run.id) or result or run

    def _reclaim_if_owner_is_dead(self, run_id: str) -> bool:
        """Orphan a turn whose owning process is conclusively gone or PID-reused.

        Deliberately not a timeout: elapsed time cannot tell a crashed owner from a model that is
        simply taking a long time, and stealing a live owner's turn is the failure mode worth
        avoiding. The creation time is checked alongside the PID because the number on its own can
        belong to an unrelated process by now.
        """

        owner = self.repos.runs.turn_owner(run_id)
        if owner is None:
            return False
        turn_id, pid, create_time = owner
        if pid <= 0 or pid == os.getpid():
            return False
        # A turn lease stores exactly PID + creation time. Validate exactly that evidence; building a
        # fake full ProcessIdentity with empty executable/command hashes makes every live owner look
        # mismatched and lets a second process steal its session.
        status = run_process_status(pid, create_time)
        if status == PID_ALIVE:
            return False  # a live owner keeps its turn, however long it takes
        if status not in {PID_GONE, PID_REUSED}:
            return False  # unknown identity is fail-closed; never clear it automatically
        if not self.repos.runs.clear_dead_turn(run_id, turn_id=turn_id):
            return False
        recovered = self.repos.runs.get(run_id)
        if recovered is not None:
            self.reconcile_terminal_bundle(
                recovered,
                target_status=RunStatus.ORPHANED,
                expected={RunStatus.ORPHANED},
                failure_type="turn_owner_pid_gone"
                if status == PID_GONE
                else "turn_owner_pid_reused",
                reason="follow-up turn owner process was lost; session not reattached",
                terminal_data={"pid_status": status, "turn_id": turn_id},
            )
        return True

    async def _resume_locked(
        self, run: Run, agent: AgentProfile, prompt: str, on_event: EventHook | None
    ) -> Run:
        session_id = run.provider_session_id
        assert session_id is not None  # guaranteed by resume()'s guard, before the lock
        run_dir = self.run_dir_for(run)
        # Per-turn artifacts (item 18): captured live so turn_NNN.md holds only THIS turn's summary,
        # usage, and tests — the cumulative view is rebuilt separately for result.json.
        turn_art = RunArtifacts()
        turn_state: dict[str, Any] = {"terminal": None}
        cancel = self.cancellations.create(run.id)
        cancel.bind()
        cli_type = agent.runtime.cli or ""
        adapter = build_cli_adapter(cli_type)
        self._cli_adapters[run.id] = adapter
        workspace_root = Path(run.worktree or run.workspace)
        # Resolved fresh for this turn, never read back from the run record. A run stores the
        # *reference* (which CLI, which variables) and never the value, so a key rotated between
        # turns takes effect here and a stale secret cannot be resurrected from persisted state.
        plan = self._cli_credential_plan(cli_type, run.id)
        request = CliRunRequest(
            run_id=run.id,
            prompt=prompt,
            workspace=workspace_root,
            permission_profile=run.permission_profile,
            credential_env=plan.as_child_env(),
            session_id=session_id,
            artifacts_dir=run_dir,
            model=agent.runtime.model or None,
            reasoning_effort=agent.runtime.reasoning_effort,
        )
        try:
            event_log = EventLog(run_dir, index=self.repos.event_index)
            event_start = (
                sum(1 for _ in event_log.read()) + 1
            )  # 1-based index of the turn's first event

            # Mark the run active again for this turn; a new turn starts with a clean failure slate.
            run.status = RunStatus.RUNNING
            run.turns += 1
            run.completed_at = None
            run.failure_type = None
            self._save_progress(run)

            def sink(event: NormalizedEvent) -> None:
                # Buffer the turn's terminal event (§4.2): it must be the LAST log entry for the turn,
                # written only after finalization, so a diff/artifact failure can still invalidate a
                # buffered "completed" before it is ever recorded.
                if _is_terminal_event(event):
                    _capture(event, turn_art, run, turn_state)
                    turn_state["pending_terminal"] = event
                    return
                saved = event_log.append(event)
                _capture(saved, turn_art, run, turn_state)
                self._persist_progress(saved, run)
                if on_event is not None:
                    on_event(saved)

            # The turn boundary: the console groups everything after this under "Turn N" (item 20).
            sink(
                NormalizedEvent(
                    run_id=run.id,
                    type=EventType.SESSION_RESUMED,
                    source="openagent",
                    data={"session_id": session_id, "turn": run.turns, "prompt": prompt},
                )
            )
            sink(
                NormalizedEvent(
                    run_id=run.id,
                    type=EventType.RUN_PHASE,
                    source="openagent",
                    data={"phase": RunPhase.RUNNING.value, "turn": run.turns},
                )
            )
            try:
                async for event in adapter.resume_run(session_id, prompt, request):
                    sink(event)
            except RunCancelled:
                turn_state["terminal"] = RunStatus.CANCELLED
                self._cancelled.add(run.id)
                run.failure_type = "user_cancelled"
                turn_state["pending_terminal"] = None  # synthesize a clean run.cancelled below
            except Exception as exc:  # noqa: BLE001 - a failed resume is a persisted failure (item 13)
                self._fail(
                    run, sink, turn_state, exc
                )  # buffers a run.failed as the pending terminal

            # ---- finalize (items 1 + 9.4): finalizing phase + diff BEFORE the single terminal event.
            sink(
                NormalizedEvent(
                    run_id=run.id,
                    type=EventType.RUN_PHASE,
                    source="openagent",
                    data={"phase": RunPhase.FINALIZING.value},
                )
            )
            self._finalize_resume(
                run, prompt, turn_state, turn_art, event_start, event_log, on_event
            )
            return run
        except _ConcurrentRunUpdate:
            return self.repos.runs.get(run.id) or run
        except Exception as exc:  # noqa: BLE001 - the outer lifecycle boundary (§4.3)
            return self._finalize_resume_failure(run, run_dir, prompt, turn_state, exc)
        finally:
            self._cli_adapters.pop(run.id, None)
            self.cancellations.discard(run.id)
            self._cancelled.discard(run.id)
            # The turn's credentials leave the redaction registry with the turn. Holding them for
            # the life of the process would keep secrets resident long after the child that needed
            # them exited, and would redact the next turn's output against a stale key.
            release_secret_scope(run.id)

    def _finalize_resume(
        self,
        run: Run,
        prompt: str,
        turn_state: dict,
        turn_art: RunArtifacts,
        event_start: int,
        event_log: EventLog,
        on_event: EventHook | None,
    ) -> None:
        """Resolve the turn, write **all** artifacts, then append the single terminal event last.

        Cumulative ``result.json``/``timeline.md`` are rebuilt from the whole event log so a failed
        turn never erases prior successful work; ``turn_NNN.md`` is scoped to this turn. Ordering
        mirrors :meth:`execute` exactly: artifacts are written *before* the terminal event, so a
        failed artifact write is caught (by the outer boundary) before the log ever says "done".
        """

        art, _ = self._rebuild_artifacts(run)
        projection = self.projection(run.id)

        # diff/changed_files are inside the boundary (§4.3): a finalization failure is its own
        # terminal state and invalidates an otherwise-completing turn (item 1) without masking an
        # earlier failure/cancellation (item 5).
        wt = WorktreeManager(self.paths.project_root, self.paths.worktrees_dir)
        ws = self._reconstruct_workspace(run)
        try:
            art.diff = wt.diff(ws)
            art.files_changed = wt.changed_files(ws)
            run.files_changed = art.files_changed
        except Exception as exc:  # noqa: BLE001 - a finalization failure is its own terminal state
            if turn_state.get("terminal") in (None, RunStatus.COMPLETED):
                turn_state["terminal"] = RunStatus.FAILED
                run.failure_type = "finalization_failed"
                turn_state["pending_terminal"] = None
                if not turn_art.error:
                    turn_art.error = {
                        "error_type": "finalization_failed",
                        "message": str(exc) or exc.__class__.__name__,
                        "phase": RunPhase.FINALIZING.value,
                        "source": "openagent",
                    }

        final = self._resolve_final(run, turn_state)
        previous_status = run.status
        run.status = final
        run.phase = final.value if final.value in _PHASE_VALUES else RunPhase.FAILED.value
        run.completed_at = _now()
        if final is RunStatus.COMPLETED:
            run.failure_type = None  # a successful new turn clears a prior turn's failure (item 18)
            if not turn_art.summary:
                turn_art.summary = projection.final_message or "Turn completed."
        self._save_transition(run, previous_status)

        pending = turn_state.get("pending_terminal")
        if pending is not None and _status_of_terminal(pending) is final:
            terminal_event = pending  # keep the backend's richer terminal data (§4.2)
        else:
            terminal_event = NormalizedEvent(
                run_id=run.id,
                type=_terminal_event_type(final),
                source="openagent",
                data={"status": final.value}
                if final is RunStatus.COMPLETED
                else {"status": final.value, "error_type": run.failure_type},
            )
        projection.apply(terminal_event)

        run_dir = self.run_dir_for(run)
        writer = ArtifactWriter(run_dir)
        # The terminal event will occupy the next slot; the turn range is inclusive through it.
        event_end = sum(1 for _ in event_log.read()) + 1
        writer.write_turn(run, prompt, turn_art, (event_start, event_end))
        writer.write_status(run)
        writer.write_results(run, art, projection)
        writer.write_expected(run, art)
        writer.write_auxiliary(art)
        writer.write_timeline(run, projection)
        writer.write_integrity(run)
        self._save_progress(run)
        try:
            saved = event_log.append(terminal_event)  # LAST log entry for the turn (item 1)
        except EventExportError:
            export_failure = {
                "stage": "event_export",
                "message": "terminal event committed; JSONL export requires repair",
            }
            art.warnings.append("events.jsonl export failed; SQLite event store is authoritative")
            writer.write_status(run, artifacts_partial=True, artifact_failure=export_failure)
            writer.write_results(
                run,
                art,
                projection,
                artifacts_partial=True,
                artifact_failure=export_failure,
            )
            writer.write_integrity(run)
            self._save_progress(run)
            turn_state["terminal_written"] = True
            return
        except Exception as exc:  # noqa: BLE001
            run.failure_type = "terminal_event_append_failed"
            raise TerminalEventAppendError("authoritative terminal event append failed") from exc
        turn_state["terminal_written"] = True
        if on_event is not None:
            with contextlib.suppress(Exception):
                on_event(saved)

    def _finalize_resume_failure(
        self, run: Run, run_dir: Path, prompt: str, turn_state: dict, exc: Exception
    ) -> Run:
        """Force a terminal state after a resume setup/finalize error (§4.3), best-effort throughout.

        Same guarantee as :meth:`_finalize_failure`: the turn never stays running, an artifact-write
        failure never looks like success, the first real error wins, and the whole recovered bundle
        is made consistent and marked partial (§5)."""

        prior = turn_state.get("terminal")
        final = prior if prior in (RunStatus.CANCELLED, RunStatus.FAILED) else RunStatus.FAILED
        previous_status = run.status
        if final is RunStatus.FAILED:
            run.failure_type = run.failure_type or "artifact_write_failed"
        run.status = final
        run.phase = final.value if final.value in _PHASE_VALUES else RunPhase.FAILED.value
        run.completed_at = _now()
        if previous_status is RunStatus.COMPLETED and final is RunStatus.FAILED:
            accepted = self.repos.runs.mark_artifact_failure(
                run,
                expected_status=previous_status,
            )
        else:
            accepted = self.repos.runs.transition_run(
                run,
                expected_statuses={previous_status},
            )
        if not accepted:
            return self.repos.runs.get(run.id) or run
        self._recover_artifacts(
            run,
            run_dir,
            exc,
            stage="resume_finalize",
            wrote_terminal=turn_state.get("terminal_written", False),
            prompt=prompt,
        )
        return run

    # ------------------------------------------------------------------ replay

    def projection(self, run_id: str, *, all_projects: bool = False) -> RunProjection:
        """Replay ``events.jsonl`` into the current projected state of a run (item 10).

        This is what lets the Run Console be *closed and reopened* — including for a run that is
        still going — and what a restarted app uses to show a live run's history before tailing it.
        """

        projection = RunProjection(run_id)
        for event in EventLog(
            self._run_dir_by_id(run_id, all_projects=all_projects),
            index=self.repos.event_index,
            run_id=run_id,
        ).read():
            projection.apply(event)
        return projection

    def is_live(self, run_id: str) -> bool:
        """Whether this process is currently executing ``run_id`` (so the console can tail it)."""

        return run_id in self._cli_adapters or self.cancellations.get(run_id) is not None

    def resume_support(self, run: Run) -> tuple[bool, str]:
        """Can this run take a follow-up turn right now? If not, why not (item 20, §5)?

        A thin wrapper over :meth:`_validate_resume` so the button and the service can never drift
        apart — the UI asks the same function that enforces the rule.
        """

        return self._validate_resume(run)

    def _validate_resume(self, run: Run) -> tuple[bool, str]:
        """The single source of truth for "may this run be resumed?" (§5, §22).

        Called by :meth:`resume_support` (which the TUI renders) **and** by :meth:`resume` itself, so
        reaching the service directly — from the CLI, a test, or any other screen — cannot bypass it.
        A UI check is not a security boundary.

        The status rules (§5):

        * ``completed`` / ``failed`` — resumable when the backend reported a session id.
        * ``cancelled`` — refused by default. The user deliberately stopped it; silently continuing
          the same session is not what "cancel" means. Start a new run.
        * ``orphaned`` — refused for **every** reason. This is the important one: ``orphaned`` is a
          terminal status, so the old "is it terminal?" check waved orphans straight through. With
          ``orphaned_unattached_process`` the backend process may still be *alive*, and resuming
          would attach a second adapter and a second process to the same session while the first is
          still running. ``pid_reused``/``pid_unknown`` cannot be reasoned about at all, and
          ``pid_gone`` would need an explicit "new run from a previous session" flow — which v0.1.3
          does not implement — so a blind resume of the same run is refused too.
        * anything non-terminal — the turn is still going; a non-interactive CLI cannot be handed
          new input mid-flight.
        """

        agent = self.repos.agents.get(run.agent)
        if agent is None:
            return False, "the agent this run used no longer exists"
        rtype = agent.runtime.type
        if rtype not in (RuntimeType.CLI, RuntimeType.CLI.value):
            return False, "follow-up is supported for CLI backends in v0.1"

        status = _status_value(run)
        if status == RunStatus.ORPHANED.value:
            return False, _ORPHAN_RESUME_REFUSAL.get(run.failure_type or "", _ORPHAN_RESUME_DEFAULT)
        if status == RunStatus.CANCELLED.value:
            return False, (
                "this run was cancelled; OpenAgent does not silently continue a session you stopped "
                "— start a new run instead"
            )
        if status not in _RESUMABLE_STATUSES:
            return False, "Follow-up becomes available after the current turn completes."
        if not run.provider_session_id:
            return False, "this backend did not report a session id, so it cannot be resumed"
        return True, ""

    def _rebuild_artifacts(self, run: Run) -> tuple[RunArtifacts, dict]:
        """Fold the full ``events.jsonl`` back into a cumulative :class:`RunArtifacts`."""

        art = RunArtifacts()
        state: dict[str, Any] = {"terminal": None}
        event_log = EventLog(self.run_dir_for(run), index=self.repos.event_index, run_id=run.id)
        usage_total: dict[str, Any] = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
        }
        cost_total: float | None = None
        saw_usage = False
        for event in event_log.read():
            _capture(event, art, run, state)
            etype = event.type if isinstance(event.type, str) else event.type.value
            if etype == EventType.USAGE_UPDATED.value:
                saw_usage = True
                for key in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                ):
                    usage_total[key] += int(event.data.get(key) or 0)
                cost = event.data.get("provider_cost")
                if (
                    cost is not None
                ):  # cumulative cost across turns (turn1 + turn2 = total, item 12)
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
            run_id=run.id,
            root=root,
            source=source,
            is_git=is_git_repo(root),
            strategy=run.worktree_strategy,
            branch=run.branch,
            base_commit=run.base_commit,
            is_copy=run.is_copy,
            in_place=run.in_place,
            baseline_dir=baseline_dir,
        )

    async def cancel(
        self,
        run_id: str,
        reason: str = "cancelled by user",
        *,
        all_projects: bool = False,
    ) -> CancelOutcome:
        """Really stop a run — API and CLI alike (item 9). Idempotent, and honest about the result.

        Paths, in order:

        * **Orphaned run** — terminal in the DB, but item 9.5 may have left its process *alive and
          unowned*. Handle it BEFORE the generic terminal short-circuit so the exact ``openagent
          cancel --id`` command recover_orphans() suggests can actually stop that process — after a
          PID + create-time identity check (§3.2). A reused/gone/unverifiable PID is never killed.
        * **API run in this process** — flip the run's :class:`RunCancellation`. The agent loop sees
          it at its next checkpoint, abandons the provider stream, stops running tools, and returns
          ``cancelled``; the executor then writes the single ``run.cancelled``. (Safe to call from a
          different event loop than the run's — that is the normal case in the TUI.)
        * **CLI run in this process** — kill the process tree; the running executor finalizes.
        * **After a restart** — no live controller, so terminate by PID (identity-verified) and
          finalize the artifacts here.

        Returns a :class:`CancelOutcome` so the CLI never prints a false "cancelled" (§3.3).
        """

        raw = self.repos.runs.get(run_id)
        if raw is None:
            return CancelOutcome.NOT_FOUND
        if not all_projects and raw.project_id is not None and raw.project_id != self.project_id:
            return CancelOutcome.WRONG_PROJECT
        run = raw

        if run.status is RunStatus.ORPHANED or _status_value(run) == RunStatus.ORPHANED.value:
            return self._cancel_orphan(run, reason)

        if run.status in _TERMINAL or _status_value(run) in {s.value for s in _TERMINAL}:
            return CancelOutcome.ALREADY_TERMINAL  # idempotent: already finished
        adapter = self._cli_adapters.get(run_id)
        if adapter is not None:
            # Prove process-tree termination before poisoning any cancellation flag. Otherwise an
            # access-denied or identity-mismatch result makes the executor synthesize cancelled.
            result = await adapter.cancel(run_id)
            if not isinstance(result, TerminationResult):
                return CancelOutcome.TERMINATION_FAILED
            if not result.verified_terminated:
                return _cancel_outcome(result)
            self._cancelled.add(run_id)
            self.cancellations.cancel(run_id, reason)
            return CancelOutcome.TERMINATED

        # A process-free API loop can be cancelled by its controller. It owns finalization.
        signalled = self.cancellations.cancel(run_id, reason)
        if signalled:
            self._cancelled.add(run_id)
            return CancelOutcome.SIGNALLED

        # Cross-process / after restart: terminate by PID with identity verification, then finalize.
        # Order matters (§6). This used to persist ``cancelled`` *unconditionally* and only then
        # report identity_mismatch — so a cancel that killed nothing still wrote run.cancelled,
        # flipped the DB and rewrote status.json, while the real process (if any) kept running. A
        # cancellation that did not happen must leave no trace: refuse first, record only on success.
        result = terminate_pid_tree(run.process_identity)
        if not result.verified_terminated:
            return _cancel_outcome(result)
        self._cancelled.add(run_id)
        self._persist_cancelled(run, reason, "user_cancelled")
        return CancelOutcome.TERMINATED

    def _cancel_orphan(self, run: Run, reason: str) -> CancelOutcome:
        """Terminate the live process behind an ``orphaned_unattached_process`` run (§3.2).

        Only this one orphan reason can have a still-running, identity-checkable process; every other
        orphan reason (pid gone/reused/unknown) is, by definition, not safely killable. Even for the
        right reason we re-verify PID + create-time identity *now* — the process may have exited or its
        PID may have been reused since recovery — and never signal a kill we did not actually perform.
        """

        if run.failure_type != "orphaned_unattached_process":
            return CancelOutcome.NOT_CANCELLABLE
        status = process_identity_status(run.process_identity)
        if status != PID_ALIVE:
            # gone / reused / unverifiable — fail closed, touch nothing.
            if status == "gone":
                return CancelOutcome.ALREADY_GONE
            if status == "unknown":
                return CancelOutcome.IDENTITY_UNKNOWN
            return CancelOutcome.IDENTITY_MISMATCH
        result = terminate_pid_tree(run.process_identity)
        if not result.verified_terminated:  # raced between the check above and the signal
            return _cancel_outcome(result)
        self._cancelled.add(run.id)
        self._persist_cancelled(run, reason, "orphaned_process_terminated_by_user")
        return CancelOutcome.TERMINATED

    def _persist_cancelled(self, run: Run, reason: str, failure_type: str) -> bool:
        """Append the single terminal ``run.cancelled`` (last), then persist status + artifact.

        The audit/termination log (if any) is written before this, so ``run.cancelled`` stays the
        final semantic event and the projection settles on ``cancelled`` (item 1).
        """

        previous = run.status
        run_dir = self.run_dir_for(run)
        # The termination audit note is written first so ``run.cancelled`` stays the final semantic
        # event (item 1).
        if run_dir.exists() and failure_type == "orphaned_process_terminated_by_user":
            self._record_orphan_terminated(run)
        # One reconciliation for the row, the terminal event and the whole bundle. This path used to
        # refresh status.json alone, leaving result.json / output.md / timeline.md / integrity.json
        # describing a run that was still going (§7.1).
        return self.reconcile_terminal_bundle(
            run,
            target_status=RunStatus.CANCELLED,
            expected={previous},
            failure_type=failure_type,
            reason=reason,
        )

    def _record_orphan_terminated(self, run: Run) -> None:
        """Audit that the orphaned live process *was* terminated by the user (mirror of the note the
        orphan recovery wrote saying it had been left running). Best-effort; ordered before
        ``run.cancelled`` so that event stays last (§3.2)."""

        run_dir = self.run_dir_for(run)
        if not run_dir.exists():
            return
        with contextlib.suppress(OSError):
            EventLog(run_dir, index=self.repos.event_index).append(
                NormalizedEvent(
                    run_id=run.id,
                    type=EventType.LOG,
                    source="openagent",
                    data={
                        "kind": "orphan",
                        "reason": "unattached_live_process",
                        "pid": run.pid,
                        "killed": True,
                        "message": (
                            f"pid {run.pid} passed a PID + create-time identity check and its "
                            "process tree was terminated at the user's request."
                        ),
                    },
                )
            )

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
        # Scoped to THIS project (spec §3.6). The database is global, so an unscoped sweep would see
        # another project's genuinely-running run, find no adapter for it in *this* process, and
        # declare it orphaned — killing the user's real work from an unrelated directory.
        for run in self.repos.runs.list_active(project_id=self.project_id):
            # Owned by *this* process (a live CLI adapter or an in-flight API/CLI cancellation
            # controller) → genuinely still running here; leave it alone.
            if run.id in self._cli_adapters or self.cancellations.get(run.id) is not None:
                continue
            status = process_identity_status(run.process_identity)
            record_live = status == PID_ALIVE
            if status == PID_ALIVE:
                failure_type = run.failure_type or "orphaned_unattached_process"
            else:
                failure_type = run.failure_type or f"orphaned_pid_{status}"
            previous = run.status
            # The audit note goes first so the terminal event stays the last semantic entry (item 1).
            if record_live:
                self._record_orphan_event(run, status, failure_type)
            # One reconciliation for the row, the terminal event and the whole artifact bundle.
            # Recovery used to update only the row and leave status.json/result.json saying
            # "running", which is how a run could report two different outcomes at once (§7.1).
            if self.reconcile_terminal_bundle(
                run,
                target_status=RunStatus.ORPHANED,
                expected={previous},
                failure_type=failure_type,
                terminal_data={"pid": run.pid, "pid_status": status, "killed": False}
                if record_live
                else {"pid_status": status},
            ):
                recovered.append(run.id)
        return recovered

    def reconcile_terminal_bundle(
        self,
        run: Run,
        *,
        target_status: RunStatus,
        expected: set[RunStatus | str] | None = None,
        failure_type: str | None = None,
        reason: str = "",
        terminal_data: dict[str, Any] | None = None,
        artifact_failure: dict | None = None,
    ) -> bool:
        """Move ``run`` to a terminal state and make **every** record of it agree (spec §7.2).

        A run's outcome is written in three places that different readers consult: the SQLite row,
        the event log, and the artifact bundle. Before v0.1.4 only the mainline completion path
        updated all three — orphan recovery updated the row and wrote a ``log`` note, and
        cross-process cancel updated the row, wrote ``run.cancelled`` and refreshed ``status.json``
        alone. A recovered run could therefore say ``orphaned`` in the database, ``running`` in
        ``result.json``, and nothing at all in the event log, and which answer you got depended on
        which file you opened.

        Every terminal route now comes through here — normal completion and failure keep their own
        richer path, but cancellation (in-process and cross-process), orphan recovery, resume
        outcomes and artifact-write recovery all land here — so there is one implementation to keep
        correct rather than five to keep in step.

        Returns ``False`` when the compare-and-set loses, meaning another actor reached a terminal
        state first; the caller must not then write anything of its own.

        Idempotent: a run that already carries a terminal event is reconciled (its bundle is
        refreshed) without a second terminal event being appended.
        """

        previous = run.status
        run.status = target_status
        run.phase = (
            target_status.value if target_status.value in _PHASE_VALUES else RunPhase.FAILED.value
        )
        run.completed_at = run.completed_at or _now()
        if failure_type is not None:
            run.failure_type = failure_type
        if target_status is RunStatus.COMPLETED:
            run.failure_type = None

        expected_from: set[RunStatus | str] = expected if expected is not None else {previous}
        if not self.repos.runs.compare_and_set_transition(run, expected=expected_from):
            return False

        run_dir = self.run_dir_for(run)
        event_log = EventLog(run_dir, index=self.repos.event_index, run_id=run.id)
        failures: list[dict[str, str]] = []
        if artifact_failure is not None:
            failures.append(
                {
                    "stage": str(artifact_failure.get("stage", "artifact")),
                    "message": str(artifact_failure.get("message", "artifact write failed")),
                    "class": "mandatory",
                }
            )

        def record_failure(stage: str, classification: str, exc: Exception) -> None:
            failures.append(
                {
                    "stage": stage,
                    "message": str(exc) or exc.__class__.__name__,
                    "class": classification,
                }
            )

        # Rebuild the cumulative bundle from the log, so the artifacts describe what actually
        # happened rather than being blanked by a recovery path that had no in-memory state.
        try:
            art, _state = self._rebuild_artifacts(run)
        except Exception as exc:  # noqa: BLE001 - record a partial recovery rather than hiding it
            record_failure("artifact_rebuild", "expected", exc)
            art = RunArtifacts()
        try:
            projection: RunProjection | None = self.projection(run.id)
        except Exception as exc:  # noqa: BLE001
            record_failure("projection_rebuild", "expected", exc)
            projection = None
        writer = ArtifactWriter(run_dir)

        mandatory: list[tuple[str, Callable[[], Any]]] = [
            ("write_status", lambda: writer.write_status(run)),
            ("write_results", lambda: writer.write_results(run, art, projection)),
        ]
        mandatory_failed = artifact_failure is not None
        for stage, step in mandatory:
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - classified and reconciled below
                mandatory_failed = True
                record_failure(stage, "mandatory", exc)

        # A success reservation is invalid when either mandatory artifact could not be materialized.
        # Correct the DB before constructing/appending a terminal event, so ``run.completed`` can
        # never be emitted for a bundle without a parseable ``result.json``.
        if mandatory_failed and run.status is RunStatus.COMPLETED:
            reserved_status = run.status
            run.status = RunStatus.FAILED
            run.phase = RunPhase.FAILED.value
            run.failure_type = "artifact_write_failed"
            run.completed_at = _now()
            if not self.repos.runs.mark_artifact_failure(
                run,
                expected_status=reserved_status,
            ):
                return False
            art.error = art.error or {
                "error_type": "artifact_write_failed",
                "message": failures[0]["message"],
                "phase": RunPhase.FAILED.value,
                "source": "openagent",
            }
            # The old projection may contain the provisional success event. Rebuild before applying
            # the real failed outcome; no terminal event has been appended yet.
            try:
                projection = self.projection(run.id)
            except Exception as exc:  # noqa: BLE001
                record_failure("projection_rebuild_failed_outcome", "expected", exc)
                projection = None

        first_failure = failures[0] if failures else None

        # Expected and optional artifacts are independent: one failure must not prevent attempts at
        # the rest. Any partial bundle gets an explicit marker in every mandatory file that can be
        # written, with the failing stage redacted by ArtifactWriter.
        expected_steps: list[tuple[str, Callable[[], Any]]] = [
            ("write_expected", lambda: writer.write_expected(run, art)),
        ]
        if projection is not None:
            expected_steps.append(
                ("write_timeline", lambda: writer.write_timeline(run, projection))
            )
        for stage, step in expected_steps:
            try:
                step()
            except Exception as exc:  # noqa: BLE001
                record_failure(stage, "expected", exc)
        try:
            writer.write_auxiliary(art)
        except Exception as exc:  # noqa: BLE001
            record_failure("write_auxiliary", "optional", exc)

        if failures:
            first_failure = failures[0]
            for stage, step in (
                (
                    "write_status_partial",
                    lambda: writer.write_status(
                        run,
                        artifacts_partial=True,
                        artifact_failure=first_failure,
                    ),
                ),
                (
                    "write_results_partial",
                    lambda: writer.write_results(
                        run,
                        art,
                        projection,
                        artifacts_partial=True,
                        artifact_failure=first_failure,
                    ),
                ),
            ):
                try:
                    step()
                except Exception as exc:  # noqa: BLE001
                    record_failure(stage, "mandatory", exc)

        # The manifest is expected and is written last so every recorded hash matches the shipped
        # bundle. If it fails, restamp status/result as partial and never leave a stale manifest.
        try:
            writer.write_integrity(run)
            self._save_progress(run)
        except _ConcurrentRunUpdate:
            return False
        except Exception as exc:  # noqa: BLE001
            record_failure("write_integrity", "expected", exc)
            with contextlib.suppress(OSError):
                (run_dir / "integrity.json").unlink()
            first_failure = failures[0]
            for step in (
                lambda: writer.write_status(
                    run, artifacts_partial=True, artifact_failure=first_failure
                ),
                lambda: writer.write_results(
                    run,
                    art,
                    projection,
                    artifacts_partial=True,
                    artifact_failure=first_failure,
                ),
            ):
                try:
                    step()
                except Exception as retry_exc:  # noqa: BLE001
                    record_failure("partial_marker_after_integrity", "mandatory", retry_exc)

        # At most one terminal event of each kind. ``orphaned -> cancelled`` remains a real second
        # transition; reconciling the same outcome twice remains idempotent.
        event_type = terminal_event_type(run.status)
        already_recorded = self.repos.event_index.has_event_type(run.id, event_type.value)
        if not already_recorded:
            data: dict[str, Any] = {"status": enum_value(run.status)}
            if run.failure_type:
                data["error_type"] = run.failure_type
            if reason:
                data["reason"] = reason
            if terminal_data:
                data.update(terminal_data)
            terminal_event = NormalizedEvent(
                run_id=run.id,
                type=event_type,
                source="openagent",
                data=data,
            )
            if projection is not None:
                projection.apply(terminal_event)
            try:
                event_log.append(terminal_event)
            except EventExportError as exc:
                raise TerminalEventExportError(
                    "terminal event committed to SQLite but JSONL export failed; run events repair"
                ) from exc
            except Exception as exc:  # noqa: BLE001 - authoritative append failure is distinct
                # If the reserved outcome was success, invalidate it and rewrite the mandatory
                # bundle before reporting the append failure. Never attempt to label this as an
                # artifact failure: it is a separate durability boundary.
                if run.status is RunStatus.COMPLETED:
                    reserved_status = run.status
                    run.status = RunStatus.FAILED
                    run.phase = RunPhase.FAILED.value
                    run.failure_type = "terminal_event_append_failed"
                    run.completed_at = _now()
                    self.repos.runs.mark_artifact_failure(
                        run,
                        expected_status=reserved_status,
                    )
                    failure = {
                        "stage": "terminal_event_append",
                        "message": "authoritative terminal event append failed",
                    }
                    with contextlib.suppress(Exception):
                        writer.write_status(run, artifacts_partial=True, artifact_failure=failure)
                        writer.write_results(
                            run,
                            art,
                            projection,
                            artifacts_partial=True,
                            artifact_failure=failure,
                        )
                raise TerminalEventAppendError(
                    "authoritative terminal event append failed"
                ) from exc
        return True

    def _record_orphan_event(self, run: Run, status: str, failure_type: str = "") -> None:
        """Write an audit event for an orphaned run whose process is still alive (item 9.5).

        Records the live PID and a safe, actionable summary — and states plainly that the process was
        **not** terminated. Best-effort: a failure to write this note must never crash recovery.
        """

        run_dir = self.run_dir_for(run)
        if not run_dir.exists():
            return
        try:
            EventLog(run_dir, index=self.repos.event_index).append(
                NormalizedEvent(
                    run_id=run.id,
                    type=EventType.LOG,
                    source="openagent",
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

    def repair_event_export(self, run_id: str, *, all_projects: bool = False) -> dict[str, Any]:
        run = self._require_run(run_id, all_projects=all_projects)
        # ``repair`` forces a full rewrite from SQLite. A plain export would try to resume from the
        # file, which is exactly what cannot be trusted when someone is asking for a repair.
        path = EventLog(self.run_dir_for(run), index=self.repos.event_index, run_id=run.id).repair()
        return {
            "run_id": run.id,
            "events": self.repos.event_index.count(run.id),
            "terminal_count": self.repos.event_index.terminal_count(run.id),
            "export": str(path),
            "repaired": True,
        }

    def rerun(
        self,
        run_id: str,
        *,
        all_projects: bool = False,
        confirm_in_place: bool = False,
    ) -> Run:
        previous = self._require_run(run_id, all_projects=all_projects)
        return self.create(
            agent_name=previous.agent,
            prompt=previous.prompt,
            worktree=previous.worktree_strategy,
            permission_profile=previous.permission_profile,
            confirm_in_place=confirm_in_place,
            execution_backend=previous.execution_backend,
            container_runtime=previous.container_runtime,
            container_image=previous.container_image,
            commit_agent_changes=previous.commit_agent_changes,
        )

    def revert_agent_commit(self, run_id: str, *, all_projects: bool = False) -> str:
        run = self._require_run(run_id, all_projects=all_projects)
        if not run.agent_commit_sha:
            raise RunError(f"run {run_id!r} has no recorded agent commit")
        workspace = self._reconstruct_workspace(run)
        project_root = Path(run.project_root or self.paths.project_root)
        state_dir = Path(run.project_state_dir or project_root / ".openagent")
        manager = WorktreeManager(project_root, state_dir / "worktrees")
        try:
            return manager.revert_commit(workspace, run.agent_commit_sha)
        except Exception as exc:
            raise RunError(f"could not revert agent commit: {exc}") from exc

    def output(self, run_id: str, fmt: str = "md", *, all_projects: bool = False) -> str:
        run_dir = self._run_dir_by_id(run_id, all_projects=all_projects)
        mapping = {
            "md": "output.md",
            "json": "result.json",
            "diff": "changes.diff",
            "logs": "logs.txt",
            "events": "events.jsonl",
            "handoff": "handoff.md",
            "status": "status.json",
            "tests": "tests.json",
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


def terminal_event_type(status: RunStatus) -> EventType:
    """The event that records reaching ``status``.

    ``ORPHANED`` maps to its own event rather than to ``run.failed`` (spec §7.3). The run did not
    fail — OpenAgent lost track of it, possibly while its backend process is still running — and the
    two need different recovery, different resume rules and different words in the UI. Folding them
    together erased a distinction the rest of the system depends on.
    """

    return {
        RunStatus.COMPLETED: EventType.RUN_COMPLETED,
        RunStatus.CANCELLED: EventType.RUN_CANCELLED,
        RunStatus.ORPHANED: EventType.RUN_ORPHANED,
    }.get(status, EventType.RUN_FAILED)


#: Backwards-compatible alias for the private name this used to have.
_terminal_event_type = terminal_event_type


_TERMINAL_EVENT_TYPES = frozenset(
    {
        EventType.RUN_COMPLETED.value,
        EventType.RUN_FAILED.value,
        EventType.RUN_CANCELLED.value,
        EventType.RUN_ORPHANED.value,
    }
)


def _is_terminal_event(event: NormalizedEvent) -> bool:
    etype = event.type if isinstance(event.type, str) else event.type.value
    return etype in _TERMINAL_EVENT_TYPES


def _status_of_terminal(event: NormalizedEvent) -> RunStatus | None:
    etype = event.type if isinstance(event.type, str) else event.type.value
    return {
        EventType.RUN_COMPLETED.value: RunStatus.COMPLETED,
        EventType.RUN_FAILED.value: RunStatus.FAILED,
        EventType.RUN_CANCELLED.value: RunStatus.CANCELLED,
        EventType.RUN_ORPHANED.value: RunStatus.ORPHANED,
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


def _agent_commit_message(agent: AgentProfile) -> str:
    model = agent.runtime.model or agent.runtime.cli or "unspecified"
    return (
        f"openagent: apply {agent.name} changes\n\n"
        f"OpenAgent-Agent: {agent.name}\n"
        f"OpenAgent-Model: {model}"
    )


def _capture(event: NormalizedEvent, art: RunArtifacts | None, run: Run, state: dict) -> None:
    etype = event.type if isinstance(event.type, str) else event.type.value
    data = event.data
    # The PID now arrives on process.started — run.started is OpenAgent's own event and has no pid.
    if etype == EventType.PROCESS_STARTED.value and data.get("pid"):
        run.pid = data["pid"]
        raw_identity = data.get("process_identity")
        run.process_identity = (
            ProcessIdentity.model_validate(raw_identity) if raw_identity is not None else None
        )
        run.pid_started_at = (
            run.process_identity.create_time if run.process_identity else data.get("create_time")
        )
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
                ran=True,
                passed=data.get("passed"),
                exit_code=data.get("exit_code"),
                command=data.get("command", ""),
            )
    elif etype in (
        EventType.FILE_CREATED.value,
        EventType.FILE_MODIFIED.value,
        EventType.FILE_DELETED.value,
    ):
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
            art.usage = {
                k: data.get(k)
                for k in ("input_tokens", "cached_input_tokens", "output_tokens", "provider_cost")
            }
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
