"""Numbered schema migrations (spec §15).

The previous "migration hook" was ``metadata.create_all()`` plus an ``UPDATE schema_meta SET
version``. ``create_all`` only ever creates *missing tables*; it does not ALTER an existing one. So
the first time a column was added to a table that already existed, an upgraded install would silently
skip the DDL, **bump the version anyway**, and then fail at runtime — while the recorded version
insisted the migration had been applied. The version was a promise nothing kept.

It also was not fail-closed in the other direction: a database written by a newer OpenAgent fell
through the ``elif`` and was opened anyway, letting old code write against a schema it does not
understand.

This module is a small, real migration runner:

* migrations are **numbered 1..N** and applied in order;
* each runs in **its own transaction**, so an interruption rolls back and the version is not moved —
  a retry is clean rather than half-applied (SQLite DDL is transactional);
* each is **idempotent** (guarded by ``PRAGMA table_info`` / ``IF NOT EXISTS``), so a fresh database
  built by ``create_all`` and an upgraded v1 database converge on the same shape;
* a **backup** of the file is taken before any upgrade runs;
* a schema newer than this build is **refused**, not opened.

Alembic would be the usual answer; it is a heavy dependency for a handful of SQLite DDL statements in
a local-first tool, so this stays small, explicit, and directly tested (``tests/unit/test_migrations.py``)
rather than assumed.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine


class SchemaTooNewError(RuntimeError):
    """The database was written by a newer OpenAgent than this one (spec §15, fail closed)."""


@dataclass
class Migration:
    version: int
    name: str
    apply: Callable[[Connection], None]


# --------------------------------------------------------------------------- helpers


def _table_exists(conn: Connection, table: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"), {"t": table}
    ).first()
    return row is not None


def _column_exists(conn: Connection, table: str, column: str) -> bool:
    if not _table_exists(conn, table):
        return False
    return any(row[1] == column for row in conn.execute(text(f"PRAGMA table_info({table})")))


def _add_column(conn: Connection, table: str, column: str, ddl: str) -> None:
    """ALTER TABLE ... ADD COLUMN, but only when it is genuinely missing.

    A fresh database is built by ``create_all`` from the *current* metadata, so it already has every
    column; an upgraded v1 database does not. The guard is what lets both paths run the same list.
    """

    if _table_exists(conn, table) and not _column_exists(conn, table, column):
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


# --------------------------------------------------------------------------- migrations


def _m001_base_schema(conn: Connection) -> None:
    """The v1 baseline. Existing v1 databases already have it; fresh ones get it from metadata."""

    from .db import metadata  # local import: db imports this module

    metadata.create_all(conn, checkfirst=True)


def _m002_run_project_scope(conn: Connection) -> None:
    """Give every run an explicit project identity and artifact location (spec §3).

    The DB is global (one file per user) while artifacts are project-local, so without this a run
    cannot say which project it belongs to, and ``output()``/``projection()`` had to guess from the
    current working directory.
    """

    _add_column(conn, "runs", "project_id", "VARCHAR")
    _add_column(conn, "runs", "project_root", "VARCHAR")
    _add_column(conn, "runs", "project_state_dir", "VARCHAR")
    _add_column(conn, "runs", "artifact_dir", "VARCHAR")
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_runs_project_id ON runs (project_id)"))

    # Backfill legacy rows from what they *did* record. An old run knows its workspace; deriving the
    # project from it keeps it attached to one project instead of matching every project (which is
    # what a NULL would do to a scoped query).
    from .projects import project_id_for  # local import: avoids a cycle at module import

    rows = conn.execute(
        text("SELECT id, workspace, worktree FROM runs WHERE project_root IS NULL")
    ).fetchall()
    for run_id, workspace, _worktree in rows:
        root = workspace or ""
        if not root:
            continue
        conn.execute(
            text("UPDATE runs SET project_id=:pid, project_root=:root WHERE id=:id"),
            {"pid": project_id_for(Path(root)), "root": str(Path(root)), "id": run_id},
        )


def _m003_model_probe_cache(conn: Connection) -> None:
    """Persist capability probes across processes (spec §7)."""

    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS model_probes ("
            " cache_key VARCHAR PRIMARY KEY,"
            " provider_id VARCHAR NOT NULL,"
            " model_id VARCHAR NOT NULL,"
            " base_url_fingerprint VARCHAR NOT NULL,"
            " credential_revision VARCHAR NOT NULL,"
            " probe_version VARCHAR NOT NULL,"
            " tested_at VARCHAR NOT NULL,"
            " data JSON NOT NULL)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_model_probes_provider "
            "ON model_probes (provider_id, model_id)"
        )
    )


def _m004_event_index_uniqueness(conn: Connection) -> None:
    """One row per (run_id, seq) (spec §14).

    Without this, two processes appending to the same run could both index the same sequence number
    and nothing would notice; the index silently diverges from events.jsonl.
    """

    if not _table_exists(conn, "events"):
        return  # nothing to constrain yet; create_all builds it with the index for fresh databases
    # Drop any pre-existing duplicates first, or the unique index cannot be created on an old DB.
    conn.execute(
        text(
            "DELETE FROM events WHERE rowid NOT IN "
            "(SELECT MIN(rowid) FROM events GROUP BY run_id, seq)"
        )
    )
    conn.execute(
        text("CREATE UNIQUE INDEX IF NOT EXISTS uq_events_run_seq ON events (run_id, seq)")
    )


MIGRATIONS: list[Migration] = [
    Migration(1, "base schema", _m001_base_schema),
    Migration(2, "run project scope", _m002_run_project_scope),
    Migration(3, "model probe cache", _m003_model_probe_cache),
    Migration(4, "event index uniqueness", _m004_event_index_uniqueness),
]

LATEST_VERSION = MIGRATIONS[-1].version


# --------------------------------------------------------------------------- runner


def _ensure_meta(conn: Connection) -> None:
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_meta (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)"
        )
    )


def current_version(conn: Connection) -> int:
    _ensure_meta(conn)
    row = conn.execute(text("SELECT value FROM schema_meta WHERE key='version'")).first()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _write_version(conn: Connection, version: int) -> None:
    conn.execute(
        text(
            "INSERT INTO schema_meta (key, value) VALUES ('version', :v) "
            "ON CONFLICT(key) DO UPDATE SET value=:v"
        ),
        {"v": str(version)},
    )


def _backup(db_path: Path, version: int) -> Path | None:
    """Copy the database aside before upgrading it (spec §15). Best-effort but reported."""

    if not db_path.exists() or db_path.stat().st_size == 0:
        return None
    target = db_path.with_suffix(db_path.suffix + f".v{version}.{int(time.time())}.bak")
    shutil.copy2(db_path, target)
    return target


def run_migrations(engine: Engine, *, db_path: Path | None = None) -> int:
    """Bring the database up to :data:`LATEST_VERSION`. Returns the version now in force.

    Refuses to touch a database from the future, applies each pending migration in its own
    transaction, and backs the file up first when there is anything to lose.
    """

    with engine.begin() as conn:
        _ensure_meta(conn)
        version = current_version(conn)

    if version > LATEST_VERSION:
        raise SchemaTooNewError(
            f"this database is at schema version {version}, but this OpenAgent only understands "
            f"{LATEST_VERSION}. It was created by a newer version — upgrade OpenAgent rather than "
            "letting an older build write to it."
        )

    pending = [m for m in MIGRATIONS if m.version > version]
    if not pending:
        return version

    # Only back up something that already holds data: a fresh install has nothing to lose.
    if db_path is not None and version > 0:
        _backup(db_path, version)

    for migration in pending:
        # One transaction per migration: an interruption rolls the DDL back AND leaves the version
        # where it was, so the retry is clean instead of half-applied.
        with engine.begin() as conn:
            migration.apply(conn)
            _write_version(conn, migration.version)
    return LATEST_VERSION
