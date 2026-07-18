"""Typed CRUD over the SQLite tables (spec §34).

Each repository serializes a Pydantic model into the ``data`` JSON column and reconstructs it on
read, keeping indexed columns in sync for querying.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..core.events import EventType, NormalizedEvent
from ..core.limits import RUNTIME_LIMITS
from ..core.models import (
    AgentProfile,
    CliInstallation,
    ModelProfile,
    Project,
    ProviderConnection,
    Run,
    RunStatus,
    Session,
)
from . import db as t
from .db import Database


class ProviderRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def upsert(self, provider: ProviderConnection) -> None:
        payload = provider.model_dump(mode="json")
        with self.db.engine.begin() as conn:
            conn.execute(
                sa_delete(t.provider_connections).where(t.provider_connections.c.id == provider.id)
            )
            conn.execute(
                insert(t.provider_connections).values(
                    id=provider.id,
                    name=provider.name,
                    provider_type=provider.provider_type,
                    enabled=1 if provider.enabled else 0,
                    data=payload,
                )
            )

    def get(self, provider_id: str) -> ProviderConnection | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(
                select(t.provider_connections.c.data).where(
                    t.provider_connections.c.id == provider_id
                )
            ).first()
        return ProviderConnection.model_validate(row[0]) if row else None

    def get_by_name(self, name: str) -> ProviderConnection | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(
                select(t.provider_connections.c.data).where(t.provider_connections.c.name == name)
            ).first()
        return ProviderConnection.model_validate(row[0]) if row else None

    def list(self) -> Sequence[ProviderConnection]:
        with self.db.engine.connect() as conn:
            rows = conn.execute(
                select(t.provider_connections.c.data).order_by(t.provider_connections.c.name)
            ).all()
        return [ProviderConnection.model_validate(r[0]) for r in rows]

    def delete(self, provider_id: str) -> None:
        with self.db.engine.begin() as conn:
            conn.execute(
                sa_delete(t.provider_connections).where(t.provider_connections.c.id == provider_id)
            )

    def delete_with_probes(self, provider_id: str) -> bool:
        with self.db.engine.begin() as conn:
            conn.execute(
                sa_delete(t.model_probes).where(t.model_probes.c.provider_id == provider_id)
            )
            result = conn.execute(
                sa_delete(t.provider_connections).where(t.provider_connections.c.id == provider_id)
            )
        return bool(result.rowcount)


class ModelRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def upsert(self, model: ModelProfile) -> None:
        with self.db.engine.begin() as conn:
            conn.execute(sa_delete(t.models).where(t.models.c.id == model.id))
            conn.execute(
                insert(t.models).values(
                    id=model.id,
                    provider_connection=model.provider_connection,
                    remote_model_id=model.remote_model_id,
                    data=model.model_dump(mode="json"),
                )
            )

    def get(self, model_id: str) -> ModelProfile | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(select(t.models.c.data).where(t.models.c.id == model_id)).first()
        return ModelProfile.model_validate(row[0]) if row else None

    def list_for_provider(self, provider_id: str) -> list[ModelProfile]:
        with self.db.engine.connect() as conn:
            rows = conn.execute(
                select(t.models.c.data).where(t.models.c.provider_connection == provider_id)
            ).all()
        return [ModelProfile.model_validate(r[0]) for r in rows]

    def delete(self, model_id: str) -> None:
        with self.db.engine.begin() as conn:
            conn.execute(sa_delete(t.models).where(t.models.c.id == model_id))


class AgentRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def upsert(self, agent: AgentProfile) -> None:
        with self.db.engine.begin() as conn:
            conn.execute(sa_delete(t.agents).where(t.agents.c.name == agent.name))
            conn.execute(
                insert(t.agents).values(
                    name=agent.name,
                    title=agent.title,
                    runtime_type=agent.runtime.type.value,
                    data=agent.model_dump(mode="json"),
                )
            )

    def get(self, name: str) -> AgentProfile | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(select(t.agents.c.data).where(t.agents.c.name == name)).first()
        return AgentProfile.model_validate(row[0]) if row else None

    def list(self) -> Sequence[AgentProfile]:
        with self.db.engine.connect() as conn:
            rows = conn.execute(select(t.agents.c.data).order_by(t.agents.c.name)).all()
        return [AgentProfile.model_validate(r[0]) for r in rows]

    def delete(self, name: str) -> bool:
        with self.db.engine.begin() as conn:
            result = conn.execute(sa_delete(t.agents).where(t.agents.c.name == name))
        return bool(result.rowcount)


class CliRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def upsert(self, cli: CliInstallation) -> None:
        with self.db.engine.begin() as conn:
            conn.execute(sa_delete(t.cli_installations).where(t.cli_installations.c.id == cli.id))
            conn.execute(
                insert(t.cli_installations).values(
                    id=cli.id,
                    type=cli.type,
                    executable=cli.executable,
                    data=cli.model_dump(mode="json"),
                )
            )

    def get(self, cli_id: str) -> CliInstallation | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(
                select(t.cli_installations.c.data).where(t.cli_installations.c.id == cli_id)
            ).first()
        return CliInstallation.model_validate(row[0]) if row else None

    def list(self) -> Sequence[CliInstallation]:
        with self.db.engine.connect() as conn:
            rows = conn.execute(
                select(t.cli_installations.c.data).order_by(t.cli_installations.c.id)
            ).all()
        return [CliInstallation.model_validate(r[0]) for r in rows]

    def delete(self, cli_id: str) -> None:
        with self.db.engine.begin() as conn:
            conn.execute(sa_delete(t.cli_installations).where(t.cli_installations.c.id == cli_id))


class ProjectRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def upsert(self, project: Project) -> None:
        values = {
            "root": project.root,
            "state": project.state,
            "marker_version": project.marker_version,
            "created_at": project.created_at.isoformat(),
            "updated_at": project.updated_at.isoformat(),
            "data": project.model_dump(mode="json"),
        }
        with self.db.engine.begin() as conn:
            result = conn.execute(
                update(t.projects).where(t.projects.c.id == project.id).values(**values)
            )
            if result.rowcount == 0:
                conn.execute(insert(t.projects).values(id=project.id, **values))

    def get(self, project_id: str) -> Project | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(
                select(t.projects.c.data).where(t.projects.c.id == project_id)
            ).first()
        return Project.model_validate(row[0]) if row else None

    def get_by_root(self, root: str) -> Project | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(select(t.projects.c.data).where(t.projects.c.root == root)).first()
        return Project.model_validate(row[0]) if row else None

    def list(self) -> Sequence[Project]:
        with self.db.engine.connect() as conn:
            rows = conn.execute(select(t.projects.c.data).order_by(t.projects.c.root)).all()
        return [Project.model_validate(row[0]) for row in rows]

    def relocate(self, project_id: str, new_root: Path) -> Project:
        project = self.get(project_id)
        if project is None:
            raise KeyError(project_id)
        old_root = Path(project.root)
        new_root = new_root.resolve()
        updated = project.model_copy(
            update={
                "root": str(new_root),
                "state": "active",
                "updated_at": datetime.now(timezone.utc),
            }
        )
        with self.db.engine.begin() as conn:
            conflict = conn.execute(
                select(t.projects.c.id).where(
                    t.projects.c.root == str(new_root), t.projects.c.id != project_id
                )
            ).first()
            if conflict:
                raise ValueError(f"project root already belongs to {conflict[0]}")
            conn.execute(
                update(t.projects)
                .where(t.projects.c.id == project_id)
                .values(
                    root=updated.root,
                    state=updated.state,
                    marker_version=updated.marker_version,
                    updated_at=updated.updated_at.isoformat(),
                    data=updated.model_dump(mode="json"),
                )
            )
            rows = conn.execute(
                select(t.runs.c.id, t.runs.c.data).where(t.runs.c.project_id == project_id)
            ).all()
            for run_id, payload in rows:
                run = Run.model_validate(payload)

                def moved(value: str | None) -> str | None:
                    if not value:
                        return value
                    try:
                        relative = Path(value).relative_to(old_root)
                    except ValueError:
                        return value
                    return str(new_root / relative)

                run.project_root = str(new_root)
                run.project_state_dir = str(new_root / ".openagent")
                run.artifact_dir = str(new_root / ".openagent" / "runs" / run_id)
                run.workspace = moved(run.workspace) or run.workspace
                run.worktree = moved(run.worktree)
                run.source_path = moved(run.source_path)
                run.baseline_dir = moved(run.baseline_dir)
                conn.execute(
                    update(t.runs)
                    .where(t.runs.c.id == run_id)
                    .values(
                        workspace=run.workspace,
                        worktree=run.worktree,
                        project_root=run.project_root,
                        project_state_dir=run.project_state_dir,
                        artifact_dir=run.artifact_dir,
                        data=run.model_dump(mode="json"),
                    )
                )
        return updated


class RunRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def create_run(self, run: Run) -> None:
        """Insert a new run. Existing lifecycle rows are never unconditionally overwritten."""

        run.state_revision = 0
        values = self._values(run, revision=0)
        values["id"] = run.id
        with self.db.engine.begin() as conn:
            conn.execute(insert(t.runs).values(**values))

    def upsert(self, run: Run) -> bool:
        """Compatibility create-or-CAS API; production lifecycle code uses explicit methods.

        Older integrations construct fixtures through ``upsert``. Preserve that surface without the
        former last-writer-wins update: a missing row is inserted, while an existing row is updated
        only when ``run.state_revision`` still matches. The method never deletes/re-inserts a row.
        """

        values = self._values(run, revision=run.state_revision)
        values["id"] = run.id
        with self.db.engine.begin() as conn:
            statement = sqlite_insert(t.runs).values(**values)
            next_revision = run.state_revision + 1
            updated = self._values(run, revision=next_revision)
            result = conn.execute(
                statement.on_conflict_do_update(
                    index_elements=[t.runs.c.id],
                    set_=updated,
                    where=t.runs.c.state_revision == run.state_revision,
                )
            )
        if result.rowcount == 1:
            # SQLite reports one row for both an insert and an accepted conflict update. A newly
            # inserted object remains revision zero; detect the stored token rather than guessing.
            stored = self.revision_of(run.id)
            if stored is not None:
                run.state_revision = stored
            return True
        return False

    def revision_of(self, run_id: str) -> int | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(select(t.runs.c.state_revision).where(t.runs.c.id == run_id)).first()
        return int(row[0]) if row else None

    def update_if_unchanged(self, run: Run, *, expected_revision: int) -> bool:
        """Persist ``run`` only if nobody else has written it since ``expected_revision``.

        Optimistic concurrency for the non-lifecycle updates (progress, metadata, session ids). A
        long-lived in-memory ``Run`` that was read before another process finished the run would
        otherwise happily write "running" back over "cancelled" — the last writer winning regardless
        of who actually knew the current state.
        """

        return self.update_progress(run, expected_revision=expected_revision)

    def update_progress(self, run: Run, *, expected_revision: int | None = None) -> bool:
        """Persist non-lifecycle metadata without changing status or accepting a stale revision."""

        expected_revision = run.state_revision if expected_revision is None else expected_revision
        next_revision = expected_revision + 1
        values = self._values(run, revision=next_revision)
        with self.db.engine.begin() as conn:
            result = conn.execute(
                update(t.runs)
                .where(
                    t.runs.c.id == run.id,
                    t.runs.c.status == run.status.value,
                    t.runs.c.state_revision == expected_revision,
                )
                .values(**values)
            )
        accepted = result.rowcount == 1
        if accepted:
            run.state_revision = next_revision
        return accepted

    @staticmethod
    def _values(run: Run, *, revision: int | None = None) -> dict:
        effective_revision = run.state_revision if revision is None else revision
        payload = run.model_dump(mode="json")
        payload["state_revision"] = effective_revision
        return {
            "agent": run.agent,
            "status": run.status.value,
            "workspace": run.workspace,
            "worktree": run.worktree,
            "provider_session_id": run.provider_session_id,
            "started_at": run.started_at.isoformat(),
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "exit_code": run.exit_code,
            "failure_type": run.failure_type,
            "project_id": run.project_id,
            "project_root": run.project_root,
            "project_state_dir": run.project_state_dir,
            "artifact_dir": run.artifact_dir,
            "pid": run.process_identity.pid if run.process_identity else run.pid,
            "process_create_time": (
                run.process_identity.create_time if run.process_identity else run.pid_started_at
            ),
            "process_executable": (
                run.process_identity.executable if run.process_identity else None
            ),
            "command_identity": (
                run.process_identity.command_identity if run.process_identity else None
            ),
            "execution_backend": run.execution_backend,
            "container_runtime": run.container_runtime,
            "container_image": run.container_image,
            "agent_commit_sha": run.agent_commit_sha,
            "state_revision": effective_revision,
            "active_turn_id": run.active_turn_id,
            "turn_owner_pid": run.turn_owner_pid,
            "turn_owner_create_time": run.turn_owner_create_time,
            "turn_started_at": run.turn_started_at,
            "turn_previous_status": run.turn_previous_status,
            "data": payload,
        }

    def compare_and_set_transition(
        self,
        run: Run,
        *,
        expected: set[RunStatus | str],
        expected_revision: int | None = None,
    ) -> bool:
        """Backward-compatible alias for :meth:`transition_run`."""

        return self.transition_run(
            run,
            expected_statuses=expected,
            expected_revision=expected_revision,
        )

    def transition_run(
        self,
        run: Run,
        *,
        expected_statuses: set[RunStatus | str],
        expected_revision: int | None = None,
    ) -> bool:
        """CAS a lifecycle transition on both status and revision, incrementing the token."""

        from ..core.lifecycle import validate_expected

        validate_expected(expected_statuses, run.status)
        expected_values = {
            value.value if isinstance(value, RunStatus) else RunStatus(value).value
            for value in expected_statuses
        }
        expected_revision = run.state_revision if expected_revision is None else expected_revision
        next_revision = expected_revision + 1
        with self.db.engine.begin() as conn:
            result = conn.execute(
                update(t.runs)
                .where(
                    t.runs.c.id == run.id,
                    t.runs.c.status.in_(expected_values),
                    t.runs.c.state_revision == expected_revision,
                )
                .values(**self._values(run, revision=next_revision))
            )
        accepted = result.rowcount == 1
        if accepted:
            run.state_revision = next_revision
        return accepted

    def mark_artifact_failure(
        self,
        run: Run,
        *,
        expected_status: RunStatus | str,
        expected_revision: int | None = None,
    ) -> bool:
        """Correct a reserved terminal outcome when mandatory bundle materialization fails.

        ``completed -> failed`` is not a public lifecycle edge. It is nevertheless required inside
        terminal reconciliation: the completed state was reserved before the mandatory artifact
        writes so a CAS loser could not write files, and a failed ``status.json``/``result.json``
        must invalidate that reservation before any success event is appended.
        """

        if run.status is not RunStatus.FAILED:
            raise ValueError("artifact failure correction must target failed")
        source = (
            expected_status.value
            if isinstance(expected_status, RunStatus)
            else str(expected_status)
        )
        expected_revision = run.state_revision if expected_revision is None else expected_revision
        next_revision = expected_revision + 1
        with self.db.engine.begin() as conn:
            result = conn.execute(
                update(t.runs)
                .where(
                    t.runs.c.id == run.id,
                    t.runs.c.status == source,
                    t.runs.c.state_revision == expected_revision,
                )
                .values(**self._values(run, revision=next_revision))
            )
        accepted = result.rowcount == 1
        if accepted:
            run.state_revision = next_revision
        return accepted

    # ------------------------------------------------------------------ turn leases (spec §8)

    def claim_turn(
        self,
        run_id: str,
        *,
        turn_id: str,
        pid: int,
        create_time: float,
        started_at: str,
        allowed_statuses: Sequence[str] = ("completed", "failed"),
    ) -> bool:
        """Atomically take ownership of the next turn for ``run_id``. One winner, in the database.

        Resume used to be guarded by an ``asyncio.Lock``, which protects one event loop in one
        process and nothing else. Two terminals, or a TUI and a CLI, could each pass that check and
        both start a backend for the same run — duplicate turn numbers, interleaved event sequences,
        two terminal events.

        The claim is a single conditional UPDATE, so the database decides the winner: only a row
        whose status is resumable *and* whose ``active_turn_id`` is still NULL can be claimed, and
        SQLite serialises the two statements. The loser sees ``rowcount == 0`` and is told a turn is
        already running.

        A crashed owner is handled by :meth:`steal_dead_turn` rather than by a timeout, because
        elapsed time cannot distinguish a dead process from a slow model.
        """

        with self.db.engine.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    select(
                        t.runs.c.status,
                        t.runs.c.state_revision,
                        t.runs.c.active_turn_id,
                        t.runs.c.data,
                    ).where(t.runs.c.id == run_id)
                ).first()
                if row is None or str(row[0]) not in allowed_statuses or row[2] is not None:
                    conn.rollback()
                    return False
                previous_status = str(row[0])
                revision = int(row[1])
                next_revision = revision + 1
                payload = self._payload(row[3])
                payload.update(
                    {
                        "status": RunStatus.RUNNING.value,
                        "phase": RunStatus.RUNNING.value,
                        "state_revision": next_revision,
                        "active_turn_id": turn_id,
                        "turn_owner_pid": pid,
                        "turn_owner_create_time": create_time,
                        "turn_started_at": started_at,
                        "turn_previous_status": previous_status,
                    }
                )
                result = conn.execute(
                    update(t.runs)
                    .where(
                        t.runs.c.id == run_id,
                        t.runs.c.status == previous_status,
                        t.runs.c.state_revision == revision,
                        t.runs.c.active_turn_id.is_(None),
                    )
                    .values(
                        status=RunStatus.RUNNING.value,
                        active_turn_id=turn_id,
                        turn_owner_pid=pid,
                        turn_owner_create_time=create_time,
                        turn_started_at=started_at,
                        turn_previous_status=previous_status,
                        state_revision=next_revision,
                        data=payload,
                    )
                )
                if result.rowcount != 1:
                    conn.rollback()
                    return False
                conn.commit()
                return True
            except BaseException:
                conn.rollback()
                raise

    def release_turn(self, run_id: str, *, turn_id: str) -> bool:
        """Give up a lease this process holds. Only the owner of ``turn_id`` may release it."""

        with self.db.engine.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    select(t.runs.c.state_revision, t.runs.c.data).where(
                        t.runs.c.id == run_id, t.runs.c.active_turn_id == turn_id
                    )
                ).first()
                if row is None:
                    conn.rollback()
                    return False
                revision = int(row[0])
                next_revision = revision + 1
                payload = self._payload(row[1])
                payload.update(
                    {
                        "state_revision": next_revision,
                        "active_turn_id": None,
                        "turn_owner_pid": None,
                        "turn_owner_create_time": None,
                        "turn_started_at": None,
                        "turn_previous_status": None,
                    }
                )
                result = conn.execute(
                    update(t.runs)
                    .where(
                        t.runs.c.id == run_id,
                        t.runs.c.active_turn_id == turn_id,
                        t.runs.c.state_revision == revision,
                    )
                    .values(
                        active_turn_id=None,
                        turn_owner_pid=None,
                        turn_owner_create_time=None,
                        turn_started_at=None,
                        turn_previous_status=None,
                        state_revision=next_revision,
                        data=payload,
                    )
                )
                if result.rowcount != 1:
                    conn.rollback()
                    return False
                conn.commit()
                return True
            except BaseException:
                conn.rollback()
                raise

    def turn_owner(self, run_id: str) -> tuple[str, int, float] | None:
        """``(turn_id, pid, create_time)`` for the current lease, or ``None`` if unleased."""

        with self.db.engine.connect() as conn:
            row = conn.execute(
                select(
                    t.runs.c.active_turn_id,
                    t.runs.c.turn_owner_pid,
                    t.runs.c.turn_owner_create_time,
                ).where(t.runs.c.id == run_id)
            ).first()
        if not row or row[0] is None:
            return None
        return str(row[0]), int(row[1] or 0), float(row[2] or 0.0)

    def clear_dead_turn(self, run_id: str, *, turn_id: str) -> bool:
        """Orphan and clear a lease whose owning process is known to be gone/reused.

        Separated from :meth:`release_turn` so the caller has to have *decided* the owner is dead —
        by checking PID and creation time, not by watching a clock. PID reuse is why the creation
        time matters: the number alone can belong to something else entirely by now.

        A crashed turn is never made resumable automatically: doing so could attach a second backend
        to a session whose first backend survived the owner. The run is terminalized as orphaned and
        both relational and JSON state move together, so it cannot remain ``running`` forever.
        """

        completed_at = datetime.now(timezone.utc).isoformat()
        with self.db.engine.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    select(
                        t.runs.c.state_revision,
                        t.runs.c.turn_previous_status,
                        t.runs.c.data,
                    ).where(t.runs.c.id == run_id, t.runs.c.active_turn_id == turn_id)
                ).first()
                if row is None:
                    conn.rollback()
                    return False
                revision = int(row[0])
                next_revision = revision + 1
                previous_status = str(row[1]) if row[1] is not None else None
                payload = self._payload(row[2])
                payload.update(
                    {
                        "status": RunStatus.ORPHANED.value,
                        "phase": RunStatus.ORPHANED.value,
                        "completed_at": completed_at,
                        "failure_type": "turn_owner_died",
                        "state_revision": next_revision,
                        "active_turn_id": None,
                        "turn_owner_pid": None,
                        "turn_owner_create_time": None,
                        "turn_started_at": None,
                        "turn_previous_status": previous_status,
                    }
                )
                result = conn.execute(
                    update(t.runs)
                    .where(
                        t.runs.c.id == run_id,
                        t.runs.c.active_turn_id == turn_id,
                        t.runs.c.state_revision == revision,
                    )
                    .values(
                        status=RunStatus.ORPHANED.value,
                        completed_at=completed_at,
                        failure_type="turn_owner_died",
                        active_turn_id=None,
                        turn_owner_pid=None,
                        turn_owner_create_time=None,
                        turn_started_at=None,
                        state_revision=next_revision,
                        data=payload,
                    )
                )
                if result.rowcount != 1:
                    conn.rollback()
                    return False
                conn.commit()
                return True
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _payload(raw: Any) -> dict[str, Any]:
        if isinstance(raw, str):
            value = json.loads(raw)
        elif isinstance(raw, dict):
            value = dict(raw)
        else:
            value = dict(raw)
        if not isinstance(value, dict):
            raise ValueError("run data payload is not an object")
        return value

    @staticmethod
    def _read_columns() -> tuple:
        return (
            t.runs.c.data,
            t.runs.c.status,
            t.runs.c.state_revision,
            t.runs.c.active_turn_id,
            t.runs.c.turn_owner_pid,
            t.runs.c.turn_owner_create_time,
            t.runs.c.turn_started_at,
            t.runs.c.turn_previous_status,
            t.runs.c.failure_type,
            t.runs.c.completed_at,
        )

    @classmethod
    def _from_row(cls, row: Sequence[Any]) -> Run:
        payload = cls._payload(row[0])
        payload.update(
            {
                "status": row[1],
                "state_revision": int(row[2] or 0),
                "active_turn_id": row[3],
                "turn_owner_pid": row[4],
                "turn_owner_create_time": row[5],
                "turn_started_at": row[6],
                "turn_previous_status": row[7],
                "failure_type": row[8],
                "completed_at": row[9],
            }
        )
        return Run.model_validate(payload)

    def get(self, run_id: str) -> Run | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(select(*self._read_columns()).where(t.runs.c.id == run_id)).first()
        return self._from_row(row) if row else None

    def list(
        self, limit: int = 50, *, project_id: str | None = None, all_projects: bool = False
    ) -> Sequence[Run]:
        """Recent runs, scoped to one project unless ``all_projects`` is asked for (spec §3.2, §3.5).

        The database is global, so an unscoped list mixes every project on the machine together.
        Legacy rows (``project_id IS NULL``, written before v0.1.3) are included in a scoped list:
        they predate scoping and hiding them would look like data loss.
        """

        query = select(*self._read_columns()).order_by(t.runs.c.started_at.desc())
        if not all_projects and project_id is not None:
            query = query.where(
                or_(t.runs.c.project_id == project_id, t.runs.c.project_id.is_(None))
            )
        with self.db.engine.connect() as conn:
            rows = conn.execute(query.limit(limit)).all()
        return [self._from_row(row) for row in rows]

    def list_active(
        self, *, project_id: str | None = None, all_projects: bool = False
    ) -> Sequence[Run]:
        """Active runs, scoped by default (spec §3.3).

        Orphan recovery reads this. Unscoped, opening OpenAgent in project B would see project A's
        running run, find no adapter for it in *this* process, and orphan it — which is the bug.

        Legacy rows (``project_id IS NULL``) are INCLUDED. They can only have been written before
        v0.1.3 (``create()`` always stamps a project now, and the v2 migration backfills from
        ``workspace``), so a row still marked active is almost certainly a genuine leftover whose
        process is long gone. Excluding them would strand them as "running" forever. This is safe
        because recovery only *marks state* — it never terminates a process — and every real run now
        carries a project_id, so the cross-project guarantee still holds for everything that matters.
        """

        active = ("queued", "starting", "running", "waiting_approval")
        query = select(*self._read_columns()).where(t.runs.c.status.in_(active))
        if not all_projects and project_id is not None:
            query = query.where(
                or_(t.runs.c.project_id == project_id, t.runs.c.project_id.is_(None))
            )
        with self.db.engine.connect() as conn:
            rows = conn.execute(query).all()
        return [self._from_row(row) for row in rows]


class SessionRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def upsert(self, session: Session) -> None:
        with self.db.engine.begin() as conn:
            conn.execute(
                sa_delete(t.sessions).where(
                    t.sessions.c.openagent_session_id == session.openagent_session_id
                )
            )
            conn.execute(
                insert(t.sessions).values(
                    openagent_session_id=session.openagent_session_id,
                    runtime=session.runtime,
                    provider_session_id=session.provider_session_id,
                    data=session.model_dump(mode="json"),
                )
            )

    def get(self, session_id: str) -> Session | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(
                select(t.sessions.c.data).where(t.sessions.c.openagent_session_id == session_id)
            ).first()
        return Session.model_validate(row[0]) if row else None


class MalformedEventBody(ValueError):
    """A durable event row exists but its JSON body cannot be normalized."""

    def __init__(self, seq: int, detail: str) -> None:
        super().__init__(f"event seq {seq} is malformed: {detail}")
        self.seq = seq
        self.detail = detail


class EventIndexRepository:
    """SQLite-authoritative event body store and per-run sequence allocator."""

    def __init__(self, database: Database) -> None:
        self.db = database

    def append(self, event_id: str, run_id: str, type_: str, timestamp: str, source: str) -> int:
        event = NormalizedEvent(
            id=event_id,
            run_id=run_id,
            type=EventType(type_),
            timestamp=timestamp,
            source=source,
            data={"legacy_append_api": True},
        )
        return self.append_event(event)

    def append_event(self, event: NormalizedEvent) -> int:
        payload = event.model_dump(mode="json")
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > RUNTIME_LIMITS.event_data_bytes:
            raise ValueError(f"event body exceeds {RUNTIME_LIMITS.event_data_bytes} bytes")
        type_ = event.type if isinstance(event.type, str) else event.type.value
        timestamp = event.timestamp
        with self.db.engine.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                state = conn.execute(
                    select(t.event_sequences.c.next_seq).where(
                        t.event_sequences.c.run_id == event.run_id
                    )
                ).first()
                seq = int(state[0]) if state else 1
                if seq > RUNTIME_LIMITS.events_per_run:
                    raise ValueError(f"run event count exceeds {RUNTIME_LIMITS.events_per_run}")
                conn.execute(
                    insert(t.events).values(
                        id=event.id,
                        run_id=event.run_id,
                        seq=seq,
                        type=type_,
                        timestamp=timestamp,
                        source=event.source,
                        body=payload,
                    )
                )
                if state:
                    conn.execute(
                        update(t.event_sequences)
                        .where(t.event_sequences.c.run_id == event.run_id)
                        .values(next_seq=seq + 1)
                    )
                else:
                    conn.execute(
                        insert(t.event_sequences).values(run_id=event.run_id, next_seq=seq + 1)
                    )
                conn.commit()
                return seq
            except BaseException:
                conn.rollback()
                raise

    def sequences_for(self, run_id: str) -> list[int]:
        """Every indexed sequence number for a run, in order. For recovery checks and tests."""

        with self.db.engine.connect() as conn:
            rows = conn.execute(
                select(t.events.c.seq).where(t.events.c.run_id == run_id).order_by(t.events.c.seq)
            ).all()
        return [r[0] for r in rows]

    def read(self, run_id: str) -> list[NormalizedEvent]:
        with self.db.engine.connect() as conn:
            rows = conn.execute(
                select(t.events.c.body).where(t.events.c.run_id == run_id).order_by(t.events.c.seq)
            ).all()
        return [NormalizedEvent.model_validate(row[0]) for row in rows]

    def iter_events_after(
        self,
        run_id: str,
        after_seq: int,
        limit: int = 500,
    ) -> Iterator[tuple[int, NormalizedEvent]]:
        """Read one keyset-paginated event batch for an authoritative live tailer.

        Unlike :meth:`iter_event_rows`, this performs exactly one bounded query. A poller controls
        when to fetch another page, so it never accidentally rereads the whole history on every
        tick. Malformed durable rows carry their sequence in :class:`MalformedEventBody`, allowing
        a tailer to report/skip the corrupt row instead of spinning on it forever.
        """

        if limit < 1:
            return
        with self.db.engine.connect() as conn:
            rows = conn.execute(
                select(t.events.c.seq, t.events.c.body)
                .where(t.events.c.run_id == run_id, t.events.c.seq > after_seq)
                .order_by(t.events.c.seq)
                .limit(limit)
            ).all()
        for seq, body in rows:
            numeric_seq = int(seq)
            try:
                event = NormalizedEvent.model_validate(body)
            except Exception as exc:  # noqa: BLE001 - normalize into a sequence-aware read error
                raise MalformedEventBody(numeric_seq, str(exc)[:500]) from exc
            yield numeric_seq, event

    def latest_activity_events(self, run_ids: Sequence[str]) -> dict[str, NormalizedEvent]:
        """Fetch the newest projection-relevant event for many runs in one indexed query."""

        if not run_ids:
            return {}
        activity_types = (
            "run.phase",
            "reasoning.summary",
            "progress.updated",
            "plan.updated",
            "command.started",
            "command.output",
            "command.completed",
            "file.created",
            "file.modified",
            "file.deleted",
            "tool.started",
            "tool.completed",
            "tool.failed",
            "web_search.started",
            "web_search.completed",
            "message.started",
            "message.delta",
            "message.completed",
        )
        latest = (
            select(t.events.c.run_id, func.max(t.events.c.seq).label("max_seq"))
            .where(t.events.c.run_id.in_(run_ids), t.events.c.type.in_(activity_types))
            .group_by(t.events.c.run_id)
            .subquery()
        )
        with self.db.engine.connect() as conn:
            rows = conn.execute(
                select(t.events.c.run_id, t.events.c.body).join(
                    latest,
                    (t.events.c.run_id == latest.c.run_id) & (t.events.c.seq == latest.c.max_seq),
                )
            ).all()
        result: dict[str, NormalizedEvent] = {}
        for run_id, body in rows:
            try:
                result[str(run_id)] = NormalizedEvent.model_validate(body)
            except Exception:  # noqa: BLE001 - Runs screen degrades to persisted phase
                continue
        return result

    def iter_event_rows(
        self, run_id: str, *, after_seq: int = 0, batch_size: int = 500
    ) -> Iterator[tuple[int, NormalizedEvent]]:
        """Stream ``(seq, event)`` in ``seq`` order, optionally resuming after a known sequence.

        Keyset pagination on ``seq`` rather than OFFSET: OFFSET makes SQLite re-scan the skipped rows
        for every page, which would reintroduce the quadratic behaviour the streaming export exists
        to remove. ``seq`` is unique per run and monotonic, so ``seq > last`` resumes in constant
        time and cannot skip or repeat a row even if the run is still being appended to.

        ``after_seq`` is what lets the JSONL export be incremental instead of rebuilding the whole
        file: the projection is append-only, so everything up to ``after_seq`` is already on disk.
        """

        last_seq = after_seq
        while True:
            with self.db.engine.connect() as conn:
                rows = conn.execute(
                    select(t.events.c.seq, t.events.c.body)
                    .where(t.events.c.run_id == run_id, t.events.c.seq > last_seq)
                    .order_by(t.events.c.seq)
                    .limit(batch_size)
                ).all()
            if not rows:
                return
            for seq, body in rows:
                last_seq = int(seq)
                yield last_seq, NormalizedEvent.model_validate(body)
            if len(rows) < batch_size:
                return

    def iter_events(
        self, run_id: str, *, after_seq: int = 0, batch_size: int = 500
    ) -> Iterator[NormalizedEvent]:
        for _seq, event in self.iter_event_rows(run_id, after_seq=after_seq, batch_size=batch_size):
            yield event

    def read_raw(self, run_id: str) -> list[dict]:
        return [event.model_dump(mode="json") for event in self.read(run_id)]

    def terminal_count(self, run_id: str) -> int:
        return len(self.terminal_types(run_id))

    def terminal_types(self, run_id: str) -> list[str]:
        """Terminal event types in durable sequence order, including duplicates.

        A count cannot distinguish a corrupt pair such as ``completed, failed`` from the one valid
        two-step terminal chain, ``orphaned, cancelled``. Doctor needs the ordered chain and must
        retain duplicates so it can reject a repeated outcome explicitly.
        """

        terminals = ("run.completed", "run.failed", "run.cancelled", "run.orphaned")
        with self.db.engine.connect() as conn:
            rows = conn.execute(
                select(t.events.c.type)
                .where(t.events.c.run_id == run_id, t.events.c.type.in_(terminals))
                .order_by(t.events.c.seq)
            ).all()
        return [str(row[0]) for row in rows]

    def has_event_type(self, run_id: str, event_type: str) -> bool:
        """Whether this run already recorded an event of exactly ``event_type``.

        Used to make terminal reconciliation idempotent without flattening genuinely distinct
        transitions: re-running orphan recovery must not append a second ``run.orphaned``, but a
        user who then cancels that orphaned run *has* changed its state, and ``run.cancelled`` must
        be recorded.
        """

        with self.db.engine.connect() as conn:
            row = conn.execute(
                select(func.count())
                .select_from(t.events)
                .where(t.events.c.run_id == run_id, t.events.c.type == event_type)
            ).first()
        return bool(row and int(row[0]))

    def count(self, run_id: str) -> int:
        with self.db.engine.connect() as conn:
            row = conn.execute(
                select(func.count()).select_from(t.events).where(t.events.c.run_id == run_id)
            ).first()
        return int(row[0]) if row else 0


class ModelProbeRepository:
    """Persisted capability probes (spec §22).

    A probe costs a real provider call, and it is the gate on ``agent add`` for a mixed catalog. The
    CLI runs every command in a *new process*, so a probe held only in memory is gone before the
    command that needs it starts — the user is told to probe, probes, and is told to probe again.

    Nothing here is derived from the secret: §22 forbids persisting the key, a hash of it, the
    Authorization header, or the raw provider response. The row records *what was tested against*
    (connection, model, endpoint, protocol, credential revision, probe version) and the verdict.
    """

    def __init__(self, database: Database) -> None:
        self.db = database

    def get(self, cache_key: str) -> dict | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(
                select(t.model_probes.c.data).where(t.model_probes.c.cache_key == cache_key)
            ).first()
        return dict(row[0]) if row else None

    def put(
        self,
        *,
        cache_key: str,
        provider_id: str,
        model_id: str,
        base_url_fingerprint: str,
        protocol: str,
        credential_revision: str,
        probe_version: str,
        tested_at: str,
        data: dict,
    ) -> None:
        with self.db.engine.begin() as conn:
            conn.execute(sa_delete(t.model_probes).where(t.model_probes.c.cache_key == cache_key))
            conn.execute(
                insert(t.model_probes).values(
                    cache_key=cache_key,
                    provider_id=provider_id,
                    model_id=model_id,
                    base_url_fingerprint=base_url_fingerprint,
                    protocol=protocol,
                    credential_revision=credential_revision,
                    probe_version=probe_version,
                    tested_at=tested_at,
                    data=data,
                )
            )

    def delete_for_provider(self, provider_id: str) -> int:
        """Drop every probe belonging to a connection — called when the connection is removed.

        A provider's id is derived from its name, so a re-add under the same name reuses the id.
        Purging here means a new connection starts with no inherited verdicts even before the
        credential-revision check gets a chance to reject them (spec §22).
        """

        with self.db.engine.begin() as conn:
            result = conn.execute(
                sa_delete(t.model_probes).where(t.model_probes.c.provider_id == provider_id)
            )
        return int(result.rowcount or 0)


class Repositories:
    """Convenience bundle of all repositories over one database."""

    def __init__(self, database: Database) -> None:
        self.db = database
        self.providers = ProviderRepository(database)
        self.models = ModelRepository(database)
        self.agents = AgentRepository(database)
        self.clis = CliRepository(database)
        self.projects = ProjectRepository(database)
        self.runs = RunRepository(database)
        self.sessions = SessionRepository(database)
        self.event_index = EventIndexRepository(database)
        self.model_probes = ModelProbeRepository(database)
