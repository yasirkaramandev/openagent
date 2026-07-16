"""Typed CRUD over the SQLite tables (spec §34).

Each repository serializes a Pydantic model into the ``data`` JSON column and reconstructs it on
read, keeping indexed columns in sync for querying.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, insert, or_, select
from sqlalchemy.exc import IntegrityError

from ..core.models import (
    AgentProfile,
    CliInstallation,
    ModelProfile,
    ProviderConnection,
    Run,
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


class RunRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def upsert(self, run: Run) -> None:
        with self.db.engine.begin() as conn:
            conn.execute(sa_delete(t.runs).where(t.runs.c.id == run.id))
            conn.execute(
                insert(t.runs).values(
                    id=run.id,
                    agent=run.agent,
                    status=run.status.value,
                    workspace=run.workspace,
                    worktree=run.worktree,
                    provider_session_id=run.provider_session_id,
                    started_at=run.started_at.isoformat(),
                    completed_at=run.completed_at.isoformat() if run.completed_at else None,
                    exit_code=run.exit_code,
                    failure_type=run.failure_type,
                    project_id=run.project_id,
                    project_root=run.project_root,
                    project_state_dir=run.project_state_dir,
                    artifact_dir=run.artifact_dir,
                    data=run.model_dump(mode="json"),
                )
            )

    def get(self, run_id: str) -> Run | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(select(t.runs.c.data).where(t.runs.c.id == run_id)).first()
        return Run.model_validate(row[0]) if row else None

    def list(
        self, limit: int = 50, *, project_id: str | None = None, all_projects: bool = False
    ) -> Sequence[Run]:
        """Recent runs, scoped to one project unless ``all_projects`` is asked for (spec §3.2, §3.5).

        The database is global, so an unscoped list mixes every project on the machine together.
        Legacy rows (``project_id IS NULL``, written before v0.1.3) are included in a scoped list:
        they predate scoping and hiding them would look like data loss.
        """

        query = select(t.runs.c.data).order_by(t.runs.c.started_at.desc())
        if not all_projects and project_id is not None:
            query = query.where(
                or_(t.runs.c.project_id == project_id, t.runs.c.project_id.is_(None))
            )
        with self.db.engine.connect() as conn:
            rows = conn.execute(query.limit(limit)).all()
        return [Run.model_validate(r[0]) for r in rows]

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
        query = select(t.runs.c.data).where(t.runs.c.status.in_(active))
        if not all_projects and project_id is not None:
            query = query.where(
                or_(t.runs.c.project_id == project_id, t.runs.c.project_id.is_(None))
            )
        with self.db.engine.connect() as conn:
            rows = conn.execute(query).all()
        return [Run.model_validate(r[0]) for r in rows]


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


class EventIndexRepository:
    """Indexes events by run (bodies live in events.jsonl)."""

    def __init__(self, database: Database) -> None:
        self.db = database

    #: How many times to re-read the sequence after losing a race for it. Each retry means another
    #: writer committed our number first; bounded so a pathological loop cannot spin forever.
    _SEQ_RETRIES = 8

    def append(self, event_id: str, run_id: str, type_: str, timestamp: str, source: str) -> int:
        """Allocate this run's next sequence number and insert the row, atomically (spec §11).

        This replaces ``next_seq()`` (a ``SELECT max(seq)+1`` on its own read connection) followed by
        a separate ``add()`` write. Read-then-write across two connections is not atomic: two
        appenders to the same run — the CLI and the TUI, or two threads of one run — both read
        ``max=N``, both compute ``N+1``, and both write it. Nothing between the two statements held
        the value, and the JSONL line is written *before* this call, so the loser of the race got an
        IntegrityError with its event already on disk: an orphan line and an index that disagrees
        with the log it indexes.

        Both statements now run in **one** ``engine.begin()`` transaction. SQLite serialises writers,
        so the allocation a writer commits is the one the next writer reads. The UNIQUE index on
        ``(run_id, seq)`` from migration m004 becomes the backstop rather than the discovery
        mechanism: a writer that still loses retries with a freshly read maximum instead of failing.

        Returns the sequence number actually allocated.
        """

        for attempt in range(self._SEQ_RETRIES):
            try:
                with self.db.engine.begin() as conn:
                    row = conn.execute(
                        select(func.max(t.events.c.seq)).where(t.events.c.run_id == run_id)
                    ).first()
                    seq = ((row[0] if row else 0) or 0) + 1
                    conn.execute(
                        insert(t.events).values(
                            id=event_id,
                            run_id=run_id,
                            seq=seq,
                            type=type_,
                            timestamp=timestamp,
                            source=source,
                        )
                    )
                return seq
            except IntegrityError:
                # Either we lost the (run_id, seq) race, or this event id is genuinely a duplicate.
                # Only the former is retryable: re-inserting a duplicate id would spin until the
                # budget ran out and then report a misleading "contention" failure.
                if self._event_exists(event_id) or attempt == self._SEQ_RETRIES - 1:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    def _event_exists(self, event_id: str) -> bool:
        with self.db.engine.connect() as conn:
            row = conn.execute(select(t.events.c.id).where(t.events.c.id == event_id)).first()
        return row is not None

    def sequences_for(self, run_id: str) -> list[int]:
        """Every indexed sequence number for a run, in order. For recovery checks and tests."""

        with self.db.engine.connect() as conn:
            rows = conn.execute(
                select(t.events.c.seq).where(t.events.c.run_id == run_id).order_by(t.events.c.seq)
            ).all()
        return [r[0] for r in rows]

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
        self.runs = RunRepository(database)
        self.sessions = SessionRepository(database)
        self.event_index = EventIndexRepository(database)
        self.model_probes = ModelProbeRepository(database)
