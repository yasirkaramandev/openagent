"""SQLite schema and connection helper (spec §34).

Each aggregate is stored as a row with a few indexed columns for querying plus a ``data`` JSON
column holding the full Pydantic model. This keeps the schema stable while models evolve, and stays
human-inspectable — a design goal of the spec. The append-only event *bodies* live in
``events.jsonl``; the ``events`` table here is only an index.

``SCHEMA_VERSION`` + a ``schema_meta`` row provide a minimal forward-only migration hook.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import (
    JSON,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine

SCHEMA_VERSION = 1

metadata = MetaData()

schema_meta = Table(
    "schema_meta", metadata,
    Column("key", String, primary_key=True),
    Column("value", String, nullable=False),
)

provider_connections = Table(
    "provider_connections", metadata,
    Column("id", String, primary_key=True),
    Column("name", String, unique=True, nullable=False),
    Column("provider_type", String, nullable=False),
    Column("enabled", Integer, nullable=False, default=1),
    Column("data", JSON, nullable=False),
)

models = Table(
    "models", metadata,
    Column("id", String, primary_key=True),
    Column("provider_connection", String, nullable=False),
    Column("remote_model_id", String, nullable=False),
    Column("data", JSON, nullable=False),
)

agents = Table(
    "agents", metadata,
    Column("name", String, primary_key=True),
    Column("title", String, nullable=False, default=""),
    Column("runtime_type", String, nullable=False),
    Column("data", JSON, nullable=False),
)

cli_installations = Table(
    "cli_installations", metadata,
    Column("id", String, primary_key=True),
    Column("type", String, nullable=False),
    Column("executable", String, nullable=False),
    Column("data", JSON, nullable=False),
)

runs = Table(
    "runs", metadata,
    Column("id", String, primary_key=True),
    Column("agent", String, nullable=False),
    Column("status", String, nullable=False),
    Column("workspace", String, nullable=False, default=""),
    Column("worktree", String),
    Column("provider_session_id", String),
    Column("started_at", String, nullable=False),
    Column("completed_at", String),
    Column("exit_code", Integer),
    Column("failure_type", String),
    Column("data", JSON, nullable=False),
)

sessions = Table(
    "sessions", metadata,
    Column("openagent_session_id", String, primary_key=True),
    Column("runtime", String, nullable=False),
    Column("provider_session_id", String),
    Column("data", JSON, nullable=False),
)

events = Table(
    "events", metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, nullable=False, index=True),
    Column("seq", Integer, nullable=False),
    Column("type", String, nullable=False),
    Column("timestamp", String, nullable=False),
    Column("source", String, nullable=False),
)

usage_records = Table(
    "usage_records", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String, nullable=False, index=True),
    Column("timestamp", String, nullable=False),
    Column("data", JSON, nullable=False),
)


class Database:
    """Thin wrapper around a SQLAlchemy engine bound to the OpenAgent SQLite file."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @classmethod
    def open(cls, db_path: Path) -> Database:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            f"sqlite:///{db_path}",
            future=True,
            json_serializer=lambda obj: obj if isinstance(obj, str) else __import__("json").dumps(obj),
        )
        db = cls(engine)
        db.migrate()
        return db

    @classmethod
    def in_memory(cls) -> Database:
        engine = create_engine("sqlite://", future=True)
        db = cls(engine)
        db.migrate()
        return db

    def migrate(self) -> None:
        """Create tables and record the schema version (forward-only)."""

        metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            row = conn.execute(
                select(schema_meta.c.value).where(schema_meta.c.key == "version")
            ).first()
            if row is None:
                conn.execute(
                    insert(schema_meta).values(key="version", value=str(SCHEMA_VERSION))
                )
            elif int(row[0]) < SCHEMA_VERSION:  # pragma: no cover - no v2 yet
                conn.execute(
                    update(schema_meta)
                    .where(schema_meta.c.key == "version")
                    .values(value=str(SCHEMA_VERSION))
                )

    def writable(self) -> bool:
        """Doctor check: can we write to the DB? (spec §41)."""

        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(schema_meta)
                    .prefix_with("OR REPLACE")
                    .values(key="_probe", value="1")
                )
            return True
        except Exception:  # pragma: no cover - environment dependent
            return False
