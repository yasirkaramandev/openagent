"""Immutable SQLite revision chain with durable backup and verification."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine


class SchemaTooNewError(RuntimeError):
    pass


class UnknownRevisionError(RuntimeError):
    pass


class MigrationVerificationError(RuntimeError):
    pass


@dataclass
class Migration:
    revision: str
    down_revision: str | None
    name: str
    upgrade: Callable[[Connection], None]
    forward_only_reason: str

    @property
    def version(self) -> int:
        """Legacy integer exposed for v0.1.2 tests and diagnostics."""

        return int(self.revision)

    @property
    def apply(self) -> Callable[[Connection], None]:
        return self.upgrade

    @apply.setter
    def apply(self, value: Callable[[Connection], None]) -> None:
        self.upgrade = value


@dataclass(frozen=True)
class MigrationReport:
    from_revision: str | None
    to_revision: str
    applied: tuple[str, ...] = ()
    backup_path: Path | None = None
    integrity_check: str = "ok"
    foreign_key_violations: tuple[tuple, ...] = ()
    row_counts: dict[str, int] = field(default_factory=dict)

    @property
    def version(self) -> int:
        return int(self.to_revision)


def _table_exists(conn: Connection, table: str) -> bool:
    return (
        conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:table"),
            {"table": table},
        ).first()
        is not None
    )


def _column_exists(conn: Connection, table: str, column: str) -> bool:
    if not _table_exists(conn, table):
        return False
    return any(row[1] == column for row in conn.exec_driver_sql(f"PRAGMA table_info({table})"))


def _add_column(conn: Connection, table: str, column: str, ddl: str) -> None:
    if _table_exists(conn, table) and not _column_exists(conn, table, column):
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _m001_base_schema(conn: Connection) -> None:
    from .db import metadata

    metadata.create_all(conn, checkfirst=True)


def _m002_run_project_scope(conn: Connection) -> None:
    _add_column(conn, "runs", "project_id", "VARCHAR")
    _add_column(conn, "runs", "project_root", "VARCHAR")
    _add_column(conn, "runs", "project_state_dir", "VARCHAR")
    _add_column(conn, "runs", "artifact_dir", "VARCHAR")
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_runs_project_id ON runs (project_id)")
    from .projects import legacy_project_id_for

    if not _table_exists(conn, "runs"):
        return
    rows = conn.execute(
        text("SELECT id, workspace FROM runs WHERE project_root IS NULL")
    ).fetchall()
    for run_id, workspace in rows:
        if not workspace:
            continue
        root = str(Path(workspace))
        conn.execute(
            text("UPDATE runs SET project_id=:project_id, project_root=:root WHERE id=:run_id"),
            {
                "project_id": legacy_project_id_for(Path(root)),
                "root": root,
                "run_id": run_id,
            },
        )


def _m003_model_probe_cache(conn: Connection) -> None:
    conn.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS model_probes ("
        "cache_key VARCHAR PRIMARY KEY, provider_id VARCHAR NOT NULL, model_id VARCHAR NOT NULL, "
        "base_url_fingerprint VARCHAR NOT NULL, credential_revision VARCHAR NOT NULL, "
        "probe_version VARCHAR NOT NULL, tested_at VARCHAR NOT NULL, data JSON NOT NULL)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_model_probes_provider "
        "ON model_probes (provider_id, model_id)"
    )


def _m004_event_index_uniqueness(conn: Connection) -> None:
    if not _table_exists(conn, "events"):
        return
    conn.exec_driver_sql(
        "DELETE FROM events WHERE rowid NOT IN (SELECT MIN(rowid) FROM events GROUP BY run_id, seq)"
    )
    conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_events_run_seq ON events (run_id, seq)"
    )


def _m005_probe_protocol_and_credential_revision(conn: Connection) -> None:
    _add_column(conn, "model_probes", "protocol", "VARCHAR NOT NULL DEFAULT ''")
    if _table_exists(conn, "model_probes"):
        conn.exec_driver_sql("DELETE FROM model_probes")
    if not _table_exists(conn, "provider_connections"):
        return
    rows = conn.exec_driver_sql("SELECT id, data FROM provider_connections").fetchall()
    for provider_id, data in rows:
        payload = json.loads(data) if isinstance(data, str) else data
        if payload.get("credential_revision"):
            continue
        payload["credential_revision"] = f"legacy-{provider_id}"
        conn.execute(
            text("UPDATE provider_connections SET data=:data WHERE id=:provider_id"),
            {"data": json.dumps(payload), "provider_id": provider_id},
        )


def _m006_projects_lifecycle_process_metadata(conn: Connection) -> None:
    conn.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS projects ("
        "id VARCHAR PRIMARY KEY, root VARCHAR NOT NULL UNIQUE, state VARCHAR NOT NULL DEFAULT 'active', "
        "marker_version INTEGER NOT NULL DEFAULT 1, created_at VARCHAR NOT NULL, "
        "updated_at VARCHAR NOT NULL, data JSON NOT NULL)"
    )
    now = "1970-01-01T00:00:00+00:00"
    if _table_exists(conn, "runs"):
        from .projects import read_project_marker

        rows = conn.exec_driver_sql(
            "SELECT DISTINCT project_id, project_root FROM runs "
            "WHERE project_id IS NOT NULL AND project_root IS NOT NULL"
        ).fetchall()
        for project_id, root in rows:
            marker = read_project_marker(Path(root))
            effective_id = marker.id if marker is not None else project_id
            if effective_id != project_id:
                conn.execute(
                    text("UPDATE runs SET project_id=:new WHERE project_id=:old"),
                    {"new": effective_id, "old": project_id},
                )
            payload = json.dumps(
                {
                    "id": effective_id,
                    "root": root,
                    "state": "missing" if not Path(root).exists() else "active",
                    "marker_version": 0,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO projects "
                    "(id, root, state, marker_version, created_at, updated_at, data) "
                    "VALUES (:id, :root, :state, 0, :now, :now, :data)"
                ),
                {
                    "id": effective_id,
                    "root": root,
                    "state": "missing" if not Path(root).exists() else "active",
                    "now": now,
                    "data": payload,
                },
            )
    for column, ddl in (
        ("pid", "INTEGER"),
        ("process_create_time", "FLOAT"),
        ("process_executable", "VARCHAR"),
        ("command_identity", "VARCHAR"),
        ("execution_backend", "VARCHAR NOT NULL DEFAULT 'host-restricted'"),
        ("container_runtime", "VARCHAR"),
        ("container_image", "VARCHAR"),
        ("agent_commit_sha", "VARCHAR"),
    ):
        _add_column(conn, "runs", column, ddl)
    if _table_exists(conn, "models"):
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_models_provider_remote "
            "ON models (provider_connection, remote_model_id)"
        )


def _m007_authoritative_event_store(conn: Connection) -> None:
    if not _table_exists(conn, "events"):
        conn.exec_driver_sql(
            "CREATE TABLE events (id VARCHAR PRIMARY KEY, run_id VARCHAR NOT NULL, "
            "seq INTEGER NOT NULL, type VARCHAR NOT NULL, timestamp VARCHAR NOT NULL, "
            "source VARCHAR NOT NULL, body JSON NOT NULL)"
        )
    _add_column(conn, "events", "body", "JSON")
    # At this revision boundary JSONL was still authoritative. Import each safe, regular export once
    # so upgrading does not replace rich historical bodies with index-only placeholders.
    if _table_exists(conn, "runs") and _column_exists(conn, "runs", "artifact_dir"):
        from ..security.filesystem import SafeWorkspaceWalker, UnsafeWorkspacePath

        run_rows = conn.exec_driver_sql(
            "SELECT id, artifact_dir FROM runs WHERE artifact_dir IS NOT NULL"
        ).fetchall()
        for run_id, artifact_dir in run_rows:
            try:
                raw = SafeWorkspaceWalker(Path(artifact_dir)).read_bytes(
                    "events.jsonl", max_bytes=256 * 1024 * 1024
                )
            except (OSError, UnsafeWorkspacePath):
                continue
            parsed: list[dict] = []
            for line in raw.splitlines()[:50_000]:
                try:
                    body = json.loads(line)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(body, dict) and body.get("run_id") == run_id and body.get("id"):
                    parsed.append(body)
            if not parsed:
                continue
            conn.execute(text("DELETE FROM events WHERE run_id=:run_id"), {"run_id": run_id})
            for seq, body in enumerate(parsed, 1):
                conn.execute(
                    text(
                        "INSERT INTO events "
                        "(id, run_id, seq, type, timestamp, source, body) "
                        "VALUES (:id, :run_id, :seq, :type, :timestamp, :source, :body)"
                    ),
                    {
                        "id": body["id"],
                        "run_id": run_id,
                        "seq": seq,
                        "type": body.get("type", "log"),
                        "timestamp": body.get("timestamp", "1970-01-01T00:00:00+00:00"),
                        "source": body.get("source", "legacy"),
                        "body": json.dumps(body),
                    },
                )
    rows = conn.exec_driver_sql(
        "SELECT id, run_id, type, timestamp, source FROM events WHERE body IS NULL"
    ).fetchall()
    for event_id, run_id, type_, timestamp, source in rows:
        body = json.dumps(
            {
                "id": event_id,
                "run_id": run_id,
                "type": type_,
                "timestamp": timestamp,
                "source": source,
                "data": {"legacy_body_unavailable": True},
            }
        )
        conn.execute(
            text("UPDATE events SET body=:body WHERE id=:id"), {"body": body, "id": event_id}
        )
    conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_events_run_seq ON events (run_id, seq)"
    )
    conn.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS event_sequences ("
        "run_id VARCHAR PRIMARY KEY, next_seq INTEGER NOT NULL)"
    )
    conn.exec_driver_sql(
        "INSERT OR REPLACE INTO event_sequences (run_id, next_seq) "
        "SELECT run_id, COALESCE(MAX(seq), 0) + 1 FROM events GROUP BY run_id"
    )


_FORWARD = (
    "Local user data revisions are forward-only; restoration uses the reported online backup."
)
MIGRATIONS: list[Migration] = [
    Migration("0001", None, "base schema", _m001_base_schema, _FORWARD),
    Migration("0002", "0001", "run project scope", _m002_run_project_scope, _FORWARD),
    Migration("0003", "0002", "model probe cache", _m003_model_probe_cache, _FORWARD),
    Migration("0004", "0003", "event sequence uniqueness", _m004_event_index_uniqueness, _FORWARD),
    Migration(
        "0005",
        "0004",
        "probe protocol and credential revision",
        _m005_probe_protocol_and_credential_revision,
        _FORWARD,
    ),
    Migration(
        "0006",
        "0005",
        "projects, lifecycle and process metadata",
        _m006_projects_lifecycle_process_metadata,
        _FORWARD,
    ),
    Migration(
        "0007",
        "0006",
        "authoritative event body store",
        _m007_authoritative_event_store,
        _FORWARD,
    ),
]

LATEST_REVISION = MIGRATIONS[-1].revision
LATEST_VERSION = MIGRATIONS[-1].version
_BY_REVISION = {migration.revision: migration for migration in MIGRATIONS}


def _ensure_meta(conn: Connection) -> None:
    conn.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS schema_meta (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)"
    )


def current_revision(conn: Connection) -> str | None:
    _ensure_meta(conn)
    row = conn.exec_driver_sql("SELECT value FROM schema_meta WHERE key='revision'").first()
    if row is not None:
        revision = str(row[0])
        if revision not in _BY_REVISION:
            raise UnknownRevisionError(f"unknown database revision {revision!r}; refusing to write")
        legacy_version = conn.exec_driver_sql(
            "SELECT value FROM schema_meta WHERE key='version'"
        ).first()
        if legacy_version is not None:
            try:
                recorded_version = int(legacy_version[0])
            except (TypeError, ValueError) as exc:
                raise UnknownRevisionError(
                    f"invalid schema version mirror {legacy_version[0]!r}"
                ) from exc
            if recorded_version > LATEST_VERSION:
                raise SchemaTooNewError(
                    f"database schema {recorded_version} is newer than supported "
                    f"{LATEST_VERSION}; upgrade OpenAgent"
                )
            if recorded_version != int(revision):
                raise UnknownRevisionError(
                    f"schema revision/version mismatch: {revision} vs {recorded_version}"
                )
        return revision
    legacy = conn.exec_driver_sql("SELECT value FROM schema_meta WHERE key='version'").first()
    if legacy is None:
        return None
    try:
        version = int(legacy[0])
    except (TypeError, ValueError) as exc:
        raise UnknownRevisionError(f"invalid legacy schema version {legacy[0]!r}") from exc
    revision = f"{version:04d}"
    if revision not in _BY_REVISION:
        if version > LATEST_VERSION:
            raise SchemaTooNewError(
                f"database schema {version} is newer than supported {LATEST_VERSION}; upgrade OpenAgent"
            )
        raise UnknownRevisionError(f"unknown legacy schema version {version}")
    return revision


def current_version(conn: Connection) -> int:
    revision = current_revision(conn)
    return int(revision) if revision else 0


def _write_revision(conn: Connection, revision: str) -> None:
    for key, value in (("revision", revision), ("version", str(int(revision)))):
        conn.execute(
            text(
                "INSERT INTO schema_meta (key, value) VALUES (:key, :value) "
                "ON CONFLICT(key) DO UPDATE SET value=:value"
            ),
            {"key": key, "value": value},
        )


_CRITICAL_TABLES = ("runs", "provider_connections", "agents")


def _row_counts(conn: Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in _CRITICAL_TABLES:
        if _table_exists(conn, table):
            counts[table] = int(conn.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar() or 0)
    return counts


def _verify(conn: Connection, minimum_counts: dict[str, int]) -> tuple[str, tuple[tuple, ...]]:
    integrity = str(conn.exec_driver_sql("PRAGMA integrity_check").scalar() or "")
    if integrity.lower() != "ok":
        raise MigrationVerificationError(f"SQLite integrity_check failed: {integrity}")
    violations = tuple(tuple(row) for row in conn.exec_driver_sql("PRAGMA foreign_key_check"))
    if violations:
        raise MigrationVerificationError(f"SQLite foreign_key_check failed: {violations[:5]}")
    after = _row_counts(conn)
    for table, before in minimum_counts.items():
        if after.get(table, 0) < before:
            raise MigrationVerificationError(
                f"critical row count decreased for {table}: {before} -> {after.get(table, 0)}"
            )
    return integrity, violations


def _online_backup(db_path: Path, revision: str | None) -> Path | None:
    if not db_path.exists() or db_path.stat().st_size == 0:
        return None
    label = int(revision or "0")
    target = db_path.with_suffix(db_path.suffix + f".v{label}.{int(time.time())}.bak")
    source = sqlite3.connect(db_path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def _pending_from(revision: str | None) -> list[Migration]:
    start = (
        0
        if revision is None
        else next(
            index + 1
            for index, migration in enumerate(MIGRATIONS)
            if migration.revision == revision
        )
    )
    pending = MIGRATIONS[start:]
    predecessor = revision
    for migration in pending:
        if migration.down_revision != predecessor:
            raise UnknownRevisionError(
                f"broken revision chain: {migration.revision} expects {migration.down_revision}, "
                f"found {predecessor}"
            )
        predecessor = migration.revision
    return pending


def run_migrations(engine: Engine, *, db_path: Path | None = None) -> MigrationReport:
    with engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            _ensure_meta(conn)
            revision = current_revision(conn)
            minimum_counts = _row_counts(conn)
            _verify(conn, minimum_counts)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    pending = _pending_from(revision)
    if not pending:
        with engine.connect() as conn:
            integrity, violations = _verify(conn, minimum_counts)
            counts = _row_counts(conn)
        return MigrationReport(
            revision,
            revision or LATEST_REVISION,
            integrity_check=integrity,
            foreign_key_violations=violations,
            row_counts=counts,
        )

    backup = _online_backup(db_path, revision) if db_path is not None and revision else None
    applied: list[str] = []
    for migration in pending:
        with engine.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                migration.upgrade(conn)
                _verify(conn, minimum_counts)
                _write_revision(conn, migration.revision)
                conn.commit()
                applied.append(migration.revision)
            except BaseException:
                conn.rollback()
                raise

    with engine.connect() as conn:
        integrity, violations = _verify(conn, minimum_counts)
        counts = _row_counts(conn)
    return MigrationReport(
        revision,
        LATEST_REVISION,
        tuple(applied),
        backup,
        integrity,
        violations,
        counts,
    )
