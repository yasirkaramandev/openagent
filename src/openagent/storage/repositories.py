"""Typed CRUD over the SQLite tables (spec §34).

Each repository serializes a Pydantic model into the ``data`` JSON column and reconstructs it on
read, keeping indexed columns in sync for querying.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, insert, select

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
                    data=run.model_dump(mode="json"),
                )
            )

    def get(self, run_id: str) -> Run | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(select(t.runs.c.data).where(t.runs.c.id == run_id)).first()
        return Run.model_validate(row[0]) if row else None

    def list(self, limit: int = 50) -> Sequence[Run]:
        with self.db.engine.connect() as conn:
            rows = conn.execute(
                select(t.runs.c.data).order_by(t.runs.c.started_at.desc()).limit(limit)
            ).all()
        return [Run.model_validate(r[0]) for r in rows]

    def list_active(self) -> Sequence[Run]:
        active = ("queued", "starting", "running", "waiting_approval")
        with self.db.engine.connect() as conn:
            rows = conn.execute(select(t.runs.c.data).where(t.runs.c.status.in_(active))).all()
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

    def next_seq(self, run_id: str) -> int:
        with self.db.engine.connect() as conn:
            row = conn.execute(
                select(func.max(t.events.c.seq)).where(t.events.c.run_id == run_id)
            ).first()
        return ((row[0] if row else 0) or 0) + 1

    def add(
        self, event_id: str, run_id: str, seq: int, type_: str, timestamp: str, source: str
    ) -> None:
        with self.db.engine.begin() as conn:
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

    def count(self, run_id: str) -> int:
        with self.db.engine.connect() as conn:
            row = conn.execute(
                select(func.count()).select_from(t.events).where(t.events.c.run_id == run_id)
            ).first()
        return int(row[0]) if row else 0


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
