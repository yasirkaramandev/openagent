"""Immutable SQLite revision chain with durable backup and verification."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ..core.errors import DatabaseReaderCompatibilityError
from ..core.versioning import compare_versions, version_at_least


class SchemaTooNewError(RuntimeError):
    pass


class UnknownRevisionError(RuntimeError):
    pass


class MigrationVerificationError(RuntimeError):
    pass


class MigrationFailedError(RuntimeError):
    """A pending revision failed after backup; the complete chain was rolled back."""

    def __init__(
        self,
        from_revision: str | None,
        backup_path: Path | None,
        cause: BaseException,
    ) -> None:
        self.from_revision = from_revision
        self.backup_path = backup_path
        detail = (
            str(cause)
            if isinstance(cause, MigrationVerificationError)
            else cause.__class__.__name__
        )
        backup = f"; backup preserved at {backup_path}" if backup_path is not None else ""
        super().__init__(
            f"migration from {from_revision or 'unversioned'} failed and was rolled back: "
            f"{detail}{backup}"
        )


_ModelT = TypeVar("_ModelT", bound=BaseModel)


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


#: The ``runs`` table as v0.1.4 wants it: every column, the real project foreign key, and the turn
#: lease columns. Written once and used by the rebuild so there is a single description of "correct".
_RUNS_TARGET_DDL = """
CREATE TABLE runs_new (
    id VARCHAR NOT NULL,
    agent VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    workspace VARCHAR NOT NULL,
    worktree VARCHAR,
    provider_session_id VARCHAR,
    started_at VARCHAR NOT NULL,
    completed_at VARCHAR,
    exit_code INTEGER,
    failure_type VARCHAR,
    project_id VARCHAR REFERENCES projects (id),
    project_root VARCHAR,
    project_state_dir VARCHAR,
    artifact_dir VARCHAR,
    pid INTEGER,
    process_create_time FLOAT,
    process_executable VARCHAR,
    command_identity VARCHAR,
    execution_backend VARCHAR NOT NULL DEFAULT 'host-restricted',
    container_runtime VARCHAR,
    container_image VARCHAR,
    agent_commit_sha VARCHAR,
    state_revision INTEGER NOT NULL DEFAULT 0,
    active_turn_id VARCHAR,
    turn_owner_pid INTEGER,
    turn_owner_create_time FLOAT,
    turn_started_at VARCHAR,
    data JSON NOT NULL,
    PRIMARY KEY (id)
)
"""

#: Columns copied across the rebuild, named explicitly. ``SELECT *`` would silently reorder or drop
#: a column the day either schema changes, which is exactly the failure this migration exists to fix.
_RUNS_CARRIED_COLUMNS = (
    "id",
    "agent",
    "status",
    "workspace",
    "worktree",
    "provider_session_id",
    "started_at",
    "completed_at",
    "exit_code",
    "failure_type",
    "project_id",
    "project_root",
    "project_state_dir",
    "artifact_dir",
    "pid",
    "process_create_time",
    "process_executable",
    "command_identity",
    "execution_backend",
    "container_runtime",
    "container_image",
    "agent_commit_sha",
    "data",
)


def _m008_runs_foreign_key_and_turn_leases(conn: Connection) -> None:
    """Rebuild ``runs`` so upgraded databases match fresh ones, and add the turn lease columns.

    Two problems, one table rebuild.

    **Schema parity (§10.2).** A fresh install builds ``runs`` from the SQLAlchemy metadata, which
    declares ``project_id REFERENCES projects(id)``. An upgraded install got that column from
    ``ALTER TABLE runs ADD COLUMN`` in revision 0002 — and SQLite's ``ALTER TABLE`` cannot attach a
    foreign key. So the same OpenAgent version enforced referential integrity on one machine and not
    on another, depending only on when the user installed it. The only way to add a constraint to an
    existing SQLite table is to rebuild it, which is what this does, following SQLite's documented
    generalized ALTER TABLE procedure.

    **Turn leases (§8).** Resume was guarded by an ``asyncio.Lock``, which is invisible to a second
    process. Two terminals could resume the same run at once. The lease columns added here are what
    make the claim a single atomic UPDATE that only one process can win.

    ``PRAGMA foreign_keys`` cannot be toggled inside a transaction and the migration runner holds
    one, so the copy happens with enforcement live. That is why dangling references are reconciled
    *first*: a legacy row pointing at a project that was never recorded would otherwise abort the
    whole upgrade.
    """

    if not _table_exists(conn, "runs"):
        return
    if _column_exists(conn, "runs", "state_revision") and _has_project_foreign_key(conn):
        return  # already rebuilt (a fresh database created from current metadata)

    _reconcile_dangling_projects(conn)

    before_count = int(conn.exec_driver_sql("SELECT COUNT(*) FROM runs").scalar() or 0)
    before_ids = {row[0] for row in conn.exec_driver_sql("SELECT id FROM runs")}

    # Only copy columns the *old* table actually has; the rest take their declared defaults.
    present = [column for column in _RUNS_CARRIED_COLUMNS if _column_exists(conn, "runs", column)]
    columns = ", ".join(present)

    conn.exec_driver_sql("DROP TABLE IF EXISTS runs_new")
    conn.exec_driver_sql(_RUNS_TARGET_DDL)
    conn.exec_driver_sql(f"INSERT INTO runs_new ({columns}) SELECT {columns} FROM runs")

    after_count = int(conn.exec_driver_sql("SELECT COUNT(*) FROM runs_new").scalar() or 0)
    after_ids = {row[0] for row in conn.exec_driver_sql("SELECT id FROM runs_new")}
    if after_count != before_count or after_ids != before_ids:
        # Never swap in a table that lost a row. The transaction rolls back and the backup stands.
        raise MigrationVerificationError(
            f"runs rebuild would lose data: {before_count} rows before, {after_count} after; "
            f"{len(before_ids - after_ids)} id(s) missing"
        )

    conn.exec_driver_sql("DROP TABLE runs")
    conn.exec_driver_sql("ALTER TABLE runs_new RENAME TO runs")
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_runs_project_id ON runs (project_id)")
    # Defensive: revision 0007 creates `events` with raw DDL that omits this index, so a database
    # that reached 0007 without one would silently lack it.
    if _table_exists(conn, "events"):
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_events_run_id ON events (run_id)")


def _has_project_foreign_key(conn: Connection) -> bool:
    for row in conn.exec_driver_sql("PRAGMA foreign_key_list(runs)"):
        if str(row[2]) == "projects" and str(row[3]) == "project_id":
            return True
    return False


def _reconcile_dangling_projects(conn: Connection) -> None:
    """Make every ``runs.project_id`` satisfiable before the foreign key starts being enforced.

    A run whose project was never recorded keeps its scoping where that is possible: the project row
    is recreated from ``project_root``. Where even that is unknown the reference is cleared rather
    than the run being dropped — ``project_id IS NULL`` is already treated as a legacy row
    everywhere, so the run stays visible instead of vanishing to satisfy a constraint.
    """

    if not _table_exists(conn, "projects"):
        return
    now = "1970-01-01T00:00:00+00:00"
    dangling = conn.exec_driver_sql(
        "SELECT r.id, r.project_id, r.project_root FROM runs r "
        "LEFT JOIN projects p ON p.id = r.project_id "
        "WHERE r.project_id IS NOT NULL AND p.id IS NULL"
    ).fetchall()
    for _run_id, project_id, project_root in dangling:
        if project_root:
            payload = json.dumps(
                {
                    "id": project_id,
                    "root": project_root,
                    "state": "missing" if not Path(project_root).exists() else "active",
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
                    "id": project_id,
                    "root": project_root,
                    "state": "missing" if not Path(project_root).exists() else "active",
                    "now": now,
                    "data": payload,
                },
            )
        else:
            conn.execute(
                text("UPDATE runs SET project_id = NULL WHERE project_id = :pid"),
                {"pid": project_id},
            )
    # A project row may still be missing if its root collided with an existing one.
    conn.exec_driver_sql(
        "UPDATE runs SET project_id = NULL WHERE project_id IS NOT NULL AND project_id NOT IN "
        "(SELECT id FROM projects)"
    )


def _m009_run_revision_payload_and_previous_status(conn: Connection) -> None:
    """Keep indexed run lifecycle columns and the JSON domain payload in lockstep.

    Revision 0008 introduced the relational lease fields, but a claim updated only those columns.
    ``RunRepository.get`` reconstructed the domain object from ``data`` and could consequently read
    ``completed`` while SQLite's indexed status was ``running``. Backfill relational state into every
    JSON payload and add the previous-status evidence needed to diagnose/recover a crashed turn.
    """

    if not _table_exists(conn, "runs"):
        return
    _add_column(conn, "runs", "turn_previous_status", "VARCHAR")
    rows = conn.exec_driver_sql(
        "SELECT id, status, state_revision, active_turn_id, turn_owner_pid, "
        "turn_owner_create_time, turn_started_at, turn_previous_status, data FROM runs"
    ).fetchall()
    for (
        run_id,
        status,
        revision,
        active_turn_id,
        owner_pid,
        owner_create_time,
        turn_started_at,
        previous_status,
        raw_data,
    ) in rows:
        try:
            payload = json.loads(raw_data) if isinstance(raw_data, str) else dict(raw_data)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MigrationVerificationError(
                f"runs record {str(run_id)[:12]!r} has invalid JSON"
            ) from exc
        payload.update(
            {
                "status": status,
                "state_revision": int(revision or 0),
                "active_turn_id": active_turn_id,
                "turn_owner_pid": owner_pid,
                "turn_owner_create_time": owner_create_time,
                "turn_started_at": turn_started_at,
                "turn_previous_status": previous_status,
            }
        )
        conn.execute(
            text("UPDATE runs SET data=:data WHERE id=:run_id"),
            {"data": json.dumps(payload), "run_id": run_id},
        )


def _redacted_record_id(value: object) -> str:
    """Return a stable diagnostic identity without disclosing a user-controlled identifier."""

    digest = hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"sha256:{digest}"


def _decode_domain_json(table: str, record_id: object, raw: object) -> dict:
    try:
        value = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MigrationVerificationError(
            f"domain validation failed in {table} record {_redacted_record_id(record_id)}: "
            "invalid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise MigrationVerificationError(
            f"domain validation failed in {table} record {_redacted_record_id(record_id)}: "
            "expected a JSON object"
        )
    return dict(value)


def _m010_legacy_nvidia_provider_normalization(conn: Connection) -> None:
    """Normalize only the exact historical OpenAI/NVIDIA Build combination.

    No credential, name, model or agent row is rewritten. Probe rows are retained for row/ID
    preservation but marked invalid, so the cache fails closed while leaving an auditable record.
    """

    if not _table_exists(conn, "provider_connections"):
        return
    from ..providers.factory import is_nvidia_build_endpoint

    result = conn.exec_driver_sql(
        "SELECT id, provider_type, data FROM provider_connections WHERE provider_type='openai'"
    )
    while True:
        rows = result.fetchmany(250)
        if not rows:
            break
        for provider_id, provider_type, raw_data in rows:
            payload = _decode_domain_json("provider_connections", provider_id, raw_data)
            if provider_type != "openai" or payload.get("provider_type") != "openai":
                continue
            if not is_nvidia_build_endpoint(payload.get("base_url")):
                continue
            payload["provider_type"] = "nvidia-build"
            conn.execute(
                text(
                    "UPDATE provider_connections SET provider_type='nvidia-build', data=:data "
                    "WHERE id=:provider_id"
                ),
                {"data": json.dumps(payload), "provider_id": provider_id},
            )
            if not _table_exists(conn, "model_probes"):
                continue
            probe_rows = conn.execute(
                text("SELECT cache_key, data FROM model_probes WHERE provider_id=:provider_id"),
                {"provider_id": provider_id},
            ).fetchall()
            for cache_key, raw_probe in probe_rows:
                probe = _decode_domain_json("model_probes", cache_key, raw_probe)
                probe["probe_version"] = "invalidated-provider-normalization"
                conn.execute(
                    text(
                        "UPDATE model_probes SET probe_version=:version, data=:data "
                        "WHERE cache_key=:cache_key"
                    ),
                    {
                        "version": "invalidated-provider-normalization",
                        "data": json.dumps(probe),
                        "cache_key": cache_key,
                    },
                )


def _m011_domain_json_validation(conn: Connection) -> None:
    """Pin the domain-validation invariant at an immutable migration boundary."""

    # Some v0.1.2 databases recorded revision 0001 even though optional domain tables had never
    # been materialized. ``create_all`` is safe here: it only creates missing tables; all ALTER and
    # rebuild work remains owned by the numbered migrations above.
    from .db import metadata

    metadata.create_all(conn, checkfirst=True)
    _validate_domain_records(conn)


#: ``provider_connections`` with the uniqueness and concurrency columns v0.1.6 needs.
_PROVIDERS_TARGET_DDL = """
CREATE TABLE provider_connections_new (
    id VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    normalized_name VARCHAR NOT NULL,
    provider_type VARCHAR NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    state_revision INTEGER NOT NULL DEFAULT 0,
    updated_at VARCHAR NOT NULL DEFAULT '',
    data JSON NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (name),
    UNIQUE (normalized_name)
)
"""

#: ``agents`` with a real foreign key to the provider it binds to.
#:
#: ``provider_id`` is nullable because a CLI agent has no provider at all — the constraint is
#: "if you name a provider, it must exist", not "you must name one". ``ON DELETE RESTRICT`` is what
#: replaces the check-then-act in ProviderService.remove(), which read the agent list and then
#: deleted, leaving a window for another process to bind a new agent in between.
_AGENTS_TARGET_DDL = """
CREATE TABLE agents_new (
    name VARCHAR NOT NULL,
    normalized_name VARCHAR NOT NULL,
    title VARCHAR NOT NULL DEFAULT '',
    runtime_type VARCHAR NOT NULL,
    provider_id VARCHAR REFERENCES provider_connections (id) ON DELETE RESTRICT,
    state_revision INTEGER NOT NULL DEFAULT 0,
    updated_at VARCHAR NOT NULL DEFAULT '',
    data JSON NOT NULL,
    PRIMARY KEY (name),
    UNIQUE (normalized_name)
)
"""


def _m012_provider_agent_concurrency(conn: Connection) -> None:
    """Move provider/agent uniqueness and the agent→provider link into the database.

    Three invariants that were previously enforced — where they were enforced at all — by Python
    reading, deciding, and then writing, with no transaction spanning the two.

    **Case-insensitive uniqueness.** ``provider_connections.name`` had a byte-exact ``UNIQUE``;
    ``agents.name`` had only its primary key. So ``OpenAI`` and ``openai`` were two providers a user
    could not tell apart. The canonical form is computed in Python (see ``core.naming``) because
    SQLite's ``NOCASE`` folds ASCII only, and stored in ``normalized_name``, which carries the
    constraint.

    **The agent→provider link.** It lived only inside the agent's JSON blob, as a provider *name*.
    Nothing stopped a provider from being deleted out from under an agent, and renaming a provider
    silently broke every agent bound to it. ``provider_id`` is a real column with a real foreign
    key, so the binding survives a rename and cannot dangle.

    **Lost updates.** Neither table had a revision column, so two processes editing the same record
    both wrote and the last one silently won. ``state_revision`` is the compare-and-swap token,
    matching what ``runs`` has done since revision 0008.

    A duplicate that already exists **blocks the migration** rather than being resolved
    automatically. Picking a winner would destroy a provider the user may still be using, and the
    two records are not interchangeable — they can hold different credentials and different base
    URLs. The error names the exact rows so the user can merge them deliberately; the pre-migration
    backup is retained by the runner either way.
    """

    from ..core.naming import normalize_name

    if _table_exists(conn, "provider_connections") and not _column_exists(
        conn, "provider_connections", "normalized_name"
    ):
        _rebuild_providers(conn, normalize_name)
    if _table_exists(conn, "agents") and not _column_exists(conn, "agents", "normalized_name"):
        _rebuild_agents(conn, normalize_name)


def _reject_duplicates(rows: list[tuple[str, str]], label: str) -> None:
    """Raise when two records normalize to the same name, naming them precisely.

    ``rows`` is ``(identifier, name)``. Never deletes or renames: the user chooses which record to
    keep, because only they know which credential is the live one.
    """

    from ..core.naming import normalize_name

    groups: dict[str, list[str]] = {}
    for identifier, name in rows:
        groups.setdefault(normalize_name(name), []).append(f"{identifier} ({name!r})")
    collisions = {key: members for key, members in groups.items() if len(members) > 1}
    if not collisions:
        return
    detail = "; ".join(
        f"{key!r}: {', '.join(members)}" for key, members in sorted(collisions.items())
    )
    raise MigrationVerificationError(
        f"{label} names collide once case and Unicode form are normalized, so a uniqueness "
        f"constraint cannot be added without losing one of them: {detail}. "
        f"Rename or remove the duplicates, then upgrade again. No records were changed."
    )


def _rebuild_providers(conn: Connection, normalize) -> None:
    rows = [
        (str(row[0]), str(row[1]))
        for row in conn.exec_driver_sql("SELECT id, name FROM provider_connections")
    ]
    _reject_duplicates(rows, "provider")

    before_ids = {identifier for identifier, _ in rows}
    conn.exec_driver_sql("DROP TABLE IF EXISTS provider_connections_new")
    conn.exec_driver_sql(_PROVIDERS_TARGET_DDL)
    # The normalized value has to be present *in the INSERT*, not patched in afterwards. Copying
    # every row with a placeholder and then updating trips the UNIQUE constraint on the second row,
    # because until the updates run they all share the placeholder. The value is computed in Python,
    # so this is a row-at-a-time copy rather than INSERT ... SELECT.
    for identifier, name in rows:
        conn.execute(
            text(
                "INSERT INTO provider_connections_new "
                "(id, name, normalized_name, provider_type, enabled, state_revision, updated_at, "
                " data) "
                "SELECT id, name, :normalized, provider_type, enabled, 0, '', data "
                "FROM provider_connections WHERE id=:id"
            ),
            {"normalized": normalize(name), "id": identifier},
        )

    after_ids = {
        str(row[0]) for row in conn.exec_driver_sql("SELECT id FROM provider_connections_new")
    }
    if after_ids != before_ids:
        raise MigrationVerificationError(
            f"provider rebuild would lose data: {len(before_ids - after_ids)} id(s) missing"
        )
    conn.exec_driver_sql("DROP TABLE provider_connections")
    conn.exec_driver_sql("ALTER TABLE provider_connections_new RENAME TO provider_connections")


def _rebuild_agents(conn: Connection, normalize) -> None:
    rows = [
        (str(row[0]), str(row[1] or "{}"))
        for row in conn.exec_driver_sql("SELECT name, data FROM agents")
    ]
    _reject_duplicates([(name, name) for name, _ in rows], "agent")

    # Resolve each agent's provider *name* (what the JSON stores) to a provider id. An API agent
    # naming a provider that does not exist is left with a NULL binding rather than blocking the
    # upgrade: that agent is already broken today, and refusing to start OpenAgent over it would
    # take away the only interface that can fix it.
    providers = {
        str(row[1]): str(row[0])
        for row in conn.exec_driver_sql("SELECT id, name FROM provider_connections")
    }
    bindings: dict[str, str | None] = {}
    for name, raw in rows:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = {}
        runtime = payload.get("runtime") if isinstance(payload, dict) else None
        provider_name = runtime.get("provider") if isinstance(runtime, dict) else None
        bindings[name] = providers.get(provider_name) if isinstance(provider_name, str) else None

    before_names = {name for name, _ in rows}
    conn.exec_driver_sql("DROP TABLE IF EXISTS agents_new")
    conn.exec_driver_sql(_AGENTS_TARGET_DDL)
    # Row at a time, with normalized_name already set: a placeholder-then-update copy would violate
    # the new UNIQUE constraint as soon as there are two agents.
    for name, _ in rows:
        conn.execute(
            text(
                "INSERT INTO agents_new "
                "(name, normalized_name, title, runtime_type, provider_id, state_revision, "
                " updated_at, data) "
                "SELECT name, :normalized, title, runtime_type, :provider_id, 0, '', data "
                "FROM agents WHERE name=:name"
            ),
            {"normalized": normalize(name), "provider_id": bindings[name], "name": name},
        )

    after_names = {str(row[0]) for row in conn.exec_driver_sql("SELECT name FROM agents_new")}
    if after_names != before_names:
        raise MigrationVerificationError(
            f"agent rebuild would lose data: {len(before_names - after_names)} name(s) missing"
        )
    conn.exec_driver_sql("DROP TABLE agents")
    conn.exec_driver_sql("ALTER TABLE agents_new RENAME TO agents")


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
    Migration(
        "0008",
        "0007",
        "runs foreign key and turn leases",
        _m008_runs_foreign_key_and_turn_leases,
        _FORWARD,
    ),
    Migration(
        "0009",
        "0008",
        "run revision payload and previous turn status",
        _m009_run_revision_payload_and_previous_status,
        _FORWARD,
    ),
    Migration(
        "0010",
        "0009",
        "legacy NVIDIA provider normalization",
        _m010_legacy_nvidia_provider_normalization,
        _FORWARD,
    ),
    Migration(
        "0011",
        "0010",
        "domain JSON validation",
        _m011_domain_json_validation,
        _FORWARD,
    ),
    Migration(
        "0012",
        "0011",
        "provider and agent concurrency schema",
        _m012_provider_agent_concurrency,
        _FORWARD,
    ),
]

LATEST_REVISION = MIGRATIONS[-1].revision
LATEST_VERSION = MIGRATIONS[-1].version
_BY_REVISION = {migration.revision: migration for migration in MIGRATIONS}

#: The oldest OpenAgent whose **domain model** can safely parse the JSON aggregates this build
#: writes. This is deliberately *not* the integer schema version: fields like
#: ``ProviderConnection.credential_revision`` live inside the ``data`` blob and were added without a
#: schema migration (it landed in the v0.1.4 hardening pass), so the integer schema number stayed
#: identical while the domain shape changed. An older reader passed the schema guard and then died
#: with a raw ``ValidationError`` — the exact failure this constant closes. Bump it only when a
#: change makes strictly older readers unsafe; it is monotonic across releases.
MINIMUM_READER_VERSION = "0.1.4"

#: The lowest schema this build can read. Migrations are forward-only and every historical revision
#: is still applied on top of a fresh DB, so any recorded schema at or below ``LATEST_VERSION`` is
#: readable; the floor exists only to make the compatibility report explicit.
MINIMUM_SUPPORTED_SCHEMA = 1


def _binary_version() -> str:
    from .. import __version__

    return __version__


def _active_binary_path() -> str:
    argv0 = sys.argv[0] if sys.argv else ""
    try:
        return str(Path(argv0).resolve()) if argv0 else sys.executable
    except OSError:
        return argv0 or sys.executable


def _repair_commands() -> list[str]:
    # The universal, DB-independent repair: ``openagent update`` never instantiates the app or opens
    # the database (see cli/app.py), so an old binary that cannot read the DB can still run it.
    return ["openagent update --repair"]


def _read_meta(conn: Connection, key: str) -> str | None:
    row = conn.exec_driver_sql("SELECT value FROM schema_meta WHERE key=:key", {"key": key}).first()
    return None if row is None else str(row[0])


def _check_reader_compatibility(conn: Connection) -> None:
    """Refuse a database a newer OpenAgent wrote, from metadata alone, before any model load (§6).

    Reads only ``schema_meta`` — never a domain row — so it cannot itself trip the ValidationError it
    exists to pre-empt. The gate fires when the database records a ``minimum_reader_version`` this
    binary does not satisfy (domain drift within one schema number). The integer schema-too-new case
    is left to :func:`current_revision`'s :class:`SchemaTooNewError`, which predates this and has its
    own contract; both are rendered cleanly by the callers (spec §17).
    """

    recorded_min_reader = _read_meta(conn, "minimum_reader_version")
    if recorded_min_reader is None:
        # A fresh DB, or one last written before this feature existed. Nothing to gate on: the
        # per-record decode boundary (spec §7.3) still catches a genuinely unreadable row.
        return
    binary_version = _binary_version()
    if version_at_least(binary_version, recorded_min_reader) is not False:
        # True (satisfied) or None (unparseable) — do not brick on an unparseable version string;
        # ``__version__`` and the constant are always PEP 440, so None does not occur in practice.
        return
    schema_row = _read_meta(conn, "version")
    raise DatabaseReaderCompatibilityError(
        database_schema=int(schema_row) if schema_row and schema_row.isdigit() else None,
        supported_schema_min=MINIMUM_SUPPORTED_SCHEMA,
        supported_schema_max=LATEST_VERSION,
        database_writer_version=_read_meta(conn, "last_writer_version"),
        minimum_reader_version=recorded_min_reader,
        binary_version=binary_version,
        binary_path=_active_binary_path(),
        repair_commands=_repair_commands(),
    )


def _stamp_writer_metadata(conn: Connection) -> None:
    """Record which build last wrote, and the reader floor it implies (§6).

    ``minimum_reader_version`` is only ever raised, never lowered: an older build that predates this
    feature leaves it absent, and no build writes a value below :data:`MINIMUM_READER_VERSION`, so a
    DB carried forward monotonically declares the newest floor any writer required.
    """

    binary_version = _binary_version()
    conn.execute(
        text(
            "INSERT INTO schema_meta (key, value) VALUES ('last_writer_version', :v) "
            "ON CONFLICT(key) DO UPDATE SET value=:v"
        ),
        {"v": binary_version},
    )
    existing_floor = _read_meta(conn, "minimum_reader_version")
    if existing_floor is None or compare_versions(MINIMUM_READER_VERSION, existing_floor) == 1:
        conn.execute(
            text(
                "INSERT INTO schema_meta (key, value) VALUES ('minimum_reader_version', :v) "
                "ON CONFLICT(key) DO UPDATE SET value=:v"
            ),
            {"v": MINIMUM_READER_VERSION},
        )


def _writer_metadata_stale(conn: Connection) -> bool:
    """Whether stamping would actually change something — so a steady-state open stays read-only."""

    if _read_meta(conn, "last_writer_version") != _binary_version():
        return True
    floor = _read_meta(conn, "minimum_reader_version")
    return floor is None or compare_versions(MINIMUM_READER_VERSION, floor) == 1


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


_DOMAIN_TABLES = (
    "provider_connections",
    "models",
    "agents",
    "cli_installations",
    "projects",
    "runs",
    "model_probes",
    "sessions",
    "events",
    "event_sequences",
    "usage_records",
)

_IDENTITY_COLUMNS = {
    "provider_connections": "id",
    "models": "id",
    "agents": "name",
    "cli_installations": "id",
    "projects": "id",
    "runs": "id",
    "model_probes": "cache_key",
    "sessions": "openagent_session_id",
    "events": "id",
    "event_sequences": "run_id",
    "usage_records": "id",
}


def _row_counts(conn: Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in _DOMAIN_TABLES:
        if _table_exists(conn, table):
            counts[table] = int(conn.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar() or 0)
    return counts


def _row_ids(conn: Connection) -> dict[str, set[object]]:
    identities: dict[str, set[object]] = {}
    for table, column in _IDENTITY_COLUMNS.items():
        if _table_exists(conn, table):
            identities[table] = {
                row[0] for row in conn.exec_driver_sql(f"SELECT {column} FROM {table}")
            }
    return identities


def _validate_model(model: type[_ModelT], table: str, record_id: object, payload: dict) -> _ModelT:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        # Pydantic's normal rendering includes the invalid input. It may contain a credential
        # reference, header, URL query or provider error, so only expose the error count.
        raise MigrationVerificationError(
            f"domain validation failed in {table} record {_redacted_record_id(record_id)}: "
            f"{exc.error_count()} schema error(s)"
        ) from exc


def _validate_domain_records(conn: Connection) -> None:
    """Stream every JSON aggregate through its current Pydantic model and check mirrored IDs."""

    from ..core.events import NormalizedEvent
    from ..core.models import (
        AgentProfile,
        CliInstallation,
        ModelProbe,
        ModelProfile,
        Project,
        ProviderConnection,
        Run,
        Session,
        UsageRecord,
    )

    specs: tuple[tuple[str, str, type[BaseModel], str], ...] = (
        ("provider_connections", "id", ProviderConnection, "id"),
        ("models", "id", ModelProfile, "id"),
        ("agents", "name", AgentProfile, "name"),
        ("cli_installations", "id", CliInstallation, "id"),
        ("projects", "id", Project, "id"),
        ("runs", "id", Run, "id"),
        ("sessions", "openagent_session_id", Session, "openagent_session_id"),
    )
    for table, identity_column, model, payload_identity in specs:
        if not _table_exists(conn, table):
            continue
        result = conn.exec_driver_sql(f"SELECT {identity_column}, data FROM {table}")
        while True:
            rows = result.fetchmany(250)
            if not rows:
                break
            for record_id, raw in rows:
                payload = _decode_domain_json(table, record_id, raw)
                parsed = _validate_model(model, table, record_id, payload)
                if getattr(parsed, payload_identity) != record_id:
                    raise MigrationVerificationError(
                        f"domain identity mismatch in {table} record "
                        f"{_redacted_record_id(record_id)}"
                    )

    if _table_exists(conn, "events"):
        result = conn.exec_driver_sql(
            "SELECT id, run_id, type, timestamp, source, body FROM events ORDER BY run_id, seq"
        )
        while True:
            rows = result.fetchmany(250)
            if not rows:
                break
            for event_id, run_id, event_type, timestamp, source, raw in rows:
                payload = _decode_domain_json("events", event_id, raw)
                parsed = _validate_model(NormalizedEvent, "events", event_id, payload)
                mirrors = (
                    (parsed.id, event_id),
                    (parsed.run_id, run_id),
                    (str(parsed.type), event_type),
                    (parsed.timestamp, timestamp),
                    (parsed.source, source),
                )
                if any(left != right for left, right in mirrors):
                    raise MigrationVerificationError(
                        f"domain identity mismatch in events record {_redacted_record_id(event_id)}"
                    )

    if _table_exists(conn, "model_probes"):
        result = conn.exec_driver_sql(
            "SELECT cache_key, provider_id, model_id, base_url_fingerprint, protocol, "
            "credential_revision, probe_version, tested_at, data FROM model_probes"
        )
        while True:
            rows = result.fetchmany(250)
            if not rows:
                break
            for row in rows:
                record_id = row[0]
                payload = {
                    "cache_key": row[0],
                    "provider_id": row[1],
                    "model_id": row[2],
                    "base_url_fingerprint": row[3],
                    "protocol": row[4],
                    "credential_revision": row[5],
                    "probe_version": row[6],
                    "tested_at": row[7],
                    "data": _decode_domain_json("model_probes", record_id, row[8]),
                }
                parsed = _validate_model(ModelProbe, "model_probes", record_id, payload)
                if (
                    parsed.data.model != parsed.model_id
                    or parsed.data.probe_version != parsed.probe_version
                ):
                    raise MigrationVerificationError(
                        "domain identity mismatch in model_probes record "
                        f"{_redacted_record_id(record_id)}"
                    )

    if _table_exists(conn, "usage_records"):
        result = conn.exec_driver_sql("SELECT id, run_id, timestamp, data FROM usage_records")
        while True:
            rows = result.fetchmany(250)
            if not rows:
                break
            for record_id, run_id, timestamp, raw in rows:
                payload = {
                    "id": record_id,
                    "run_id": run_id,
                    "timestamp": timestamp,
                    "data": _decode_domain_json("usage_records", record_id, raw),
                }
                _validate_model(UsageRecord, "usage_records", record_id, payload)

    if _table_exists(conn, "event_sequences") and _table_exists(conn, "events"):
        bad = conn.exec_driver_sql(
            "SELECT s.run_id FROM event_sequences s LEFT JOIN "
            "(SELECT run_id, COALESCE(MAX(seq), 0) + 1 AS expected FROM events GROUP BY run_id) e "
            "ON e.run_id=s.run_id WHERE s.next_seq < COALESCE(e.expected, 1) LIMIT 1"
        ).first()
        if bad is not None:
            raise MigrationVerificationError(
                f"event sequence is behind durable events for record {_redacted_record_id(bad[0])}"
            )


def _verify(
    conn: Connection,
    minimum_counts: dict[str, int],
    minimum_ids: dict[str, set[object]] | None = None,
    *,
    validate_domain: bool = False,
) -> tuple[str, tuple[tuple, ...]]:
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
    if minimum_ids:
        after_ids = _row_ids(conn)
        for table, expected_ids in minimum_ids.items():
            missing = expected_ids - after_ids.get(table, set())
            if missing:
                sample = next(iter(missing))
                raise MigrationVerificationError(
                    f"domain identity disappeared from {table}: {_redacted_record_id(sample)}"
                )
    if validate_domain:
        _validate_domain_records(conn)
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
    existed_before_connect = bool(
        db_path is not None and db_path.exists() and db_path.stat().st_size > 0
    )
    with engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            _ensure_meta(conn)
            # Refuse a DB a newer OpenAgent wrote before touching a single domain row, so an old
            # binary reports "binary too old" instead of a raw ValidationError deep in a model (§6).
            _check_reader_compatibility(conn)
            revision = current_revision(conn)
            minimum_counts = _row_counts(conn)
            minimum_ids = _row_ids(conn)
            _verify(
                conn,
                minimum_counts,
                minimum_ids,
                validate_domain=revision == LATEST_REVISION,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    pending = _pending_from(revision)
    if not pending:
        with engine.connect() as conn:
            integrity, violations = _verify(conn, minimum_counts, minimum_ids, validate_domain=True)
            counts = _row_counts(conn)
            # Stamp which build last read/wrote this DB even when no migration ran, so the reader
            # floor is present for the next opener. Skipped when nothing would change, so a stable
            # DB opened repeatedly by the same build stays read-only.
            if _writer_metadata_stale(conn):
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                try:
                    _stamp_writer_metadata(conn)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
        return MigrationReport(
            revision,
            revision or LATEST_REVISION,
            integrity_check=integrity,
            foreign_key_violations=violations,
            row_counts=counts,
        )

    backup = (
        _online_backup(db_path, revision)
        if db_path is not None and existed_before_connect
        else None
    )
    applied: list[str] = []
    # The complete pending chain is atomic. Normalization must not commit if the following domain
    # validation discovers a corrupt unrelated row.
    with engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            for migration in pending:
                migration.upgrade(conn)
                _write_revision(conn, migration.revision)
                applied.append(migration.revision)
            _stamp_writer_metadata(conn)
            _verify(conn, minimum_counts, minimum_ids, validate_domain=True)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise MigrationFailedError(revision, backup, exc) from exc
        except BaseException:
            conn.rollback()
            raise

    with engine.connect() as conn:
        integrity, violations = _verify(conn, minimum_counts, minimum_ids, validate_domain=True)
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
