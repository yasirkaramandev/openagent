"""Real, numbered schema migrations (spec §15).

``Database.migrate()`` was ``metadata.create_all()`` followed by::

    elif int(row[0]) < SCHEMA_VERSION:
        conn.execute(update(schema_meta)...values(value=str(SCHEMA_VERSION)))

``create_all`` only creates *missing tables* — it never ALTERs an existing one. So the moment a column
was added to an existing table, an upgraded install would: skip the DDL, **bump the version anyway**,
and then fail at runtime on the missing column, while the recorded version claimed the migration had
been applied. The version row was a promise nothing kept.

It was also not fail-closed the other way: a DB written by a *newer* OpenAgent (version > ours) fell
through the ``elif`` and was opened regardless, letting old code write against a schema it does not
understand.

These tests pin: real DDL, one transaction per migration, a backup before upgrading, idempotency,
survival of an interrupted run, and a refusal to open a future schema.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from openagent.storage.db import Database
from openagent.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    SchemaTooNewError,
    UnknownRevisionError,
    current_revision,
    current_version,
    run_migrations,
)


def _v1_database(path: Path) -> None:
    """A v1 database exactly as v0.1.2 wrote it: the old `runs` shape, version=1, real data."""

    engine = create_engine(f"sqlite:///{path}", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE runs ("
                " id VARCHAR PRIMARY KEY, agent VARCHAR NOT NULL, status VARCHAR NOT NULL,"
                " workspace VARCHAR NOT NULL DEFAULT '', worktree VARCHAR,"
                " provider_session_id VARCHAR, started_at VARCHAR NOT NULL, completed_at VARCHAR,"
                " exit_code INTEGER, failure_type VARCHAR, data JSON NOT NULL)"
            )
        )
        # A real v1 database (built by v0.1.2's create_all) has the event index table too, with no
        # uniqueness constraint on (run_id, seq) — that is what migration 4 adds.
        conn.execute(
            text(
                "CREATE TABLE events ("
                " id VARCHAR PRIMARY KEY, run_id VARCHAR NOT NULL, seq INTEGER NOT NULL,"
                " type VARCHAR NOT NULL, timestamp VARCHAR NOT NULL, source VARCHAR NOT NULL)"
            )
        )
        conn.execute(
            text("CREATE TABLE schema_meta (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)")
        )
        conn.execute(text("INSERT INTO schema_meta (key, value) VALUES ('version', '1')"))
        payload = json.dumps(
            {
                "id": "run_old",
                "agent": "legacy",
                "status": "completed",
                "workspace": "/old/project",
                "started_at": "2026-01-01T00:00:00+00:00",
                "turns": 1,
            }
        )
        conn.execute(
            text(
                "INSERT INTO runs (id, agent, status, workspace, started_at, data)"
                " VALUES ('run_old', 'legacy', 'completed', '/old/project',"
                " '2026-01-01T00:00:00+00:00', :data)"
            ),
            {"data": payload},
        )
    engine.dispose()


def _columns(path: Path, table: str) -> set[str]:
    engine = create_engine(f"sqlite:///{path}", future=True)
    try:
        with engine.begin() as conn:
            return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
    finally:
        engine.dispose()


def test_migrations_are_numbered_contiguously_from_one():
    versions = [m.version for m in MIGRATIONS]
    assert versions == list(range(1, len(MIGRATIONS) + 1)), "migrations must be 1..N with no gaps"
    assert LATEST_VERSION == versions[-1]
    assert [m.revision for m in MIGRATIONS] == [f"{version:04d}" for version in versions]
    assert [m.down_revision for m in MIGRATIONS] == [
        None,
        *[migration.revision for migration in MIGRATIONS[:-1]],
    ]
    assert all(m.forward_only_reason for m in MIGRATIONS)


def test_upgrade_from_a_real_v1_database_applies_the_ddl(tmp_path: Path):
    """The regression: the version used to move without the schema moving with it."""

    db_path = tmp_path / "old.db"
    _v1_database(db_path)
    assert "project_id" not in _columns(db_path, "runs")

    Database.open(db_path)

    # The DDL really ran…
    columns = _columns(db_path, "runs")
    for column in ("project_id", "project_root", "project_state_dir", "artifact_dir"):
        assert column in columns, f"{column} was not added — the migration only bumped the version"
    # …and the recorded version matches reality.
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        assert current_version(conn) == LATEST_VERSION
    engine.dispose()


def test_upgrade_preserves_existing_rows(tmp_path: Path):
    """§1.8: never destroy the user's runs/providers/agents."""

    db_path = tmp_path / "old.db"
    _v1_database(db_path)
    db = Database.open(db_path)
    with db.engine.begin() as conn:
        row = conn.execute(text("SELECT id, agent, status FROM runs WHERE id='run_old'")).first()
    assert row is not None, "the migration destroyed an existing run"
    assert row[1] == "legacy" and row[2] == "completed"


def test_upgrade_backfills_project_columns_from_the_old_payload(tmp_path: Path):
    db_path = tmp_path / "old.db"
    _v1_database(db_path)
    db = Database.open(db_path)
    with db.engine.begin() as conn:
        row = conn.execute(
            text("SELECT project_root, artifact_dir FROM runs WHERE id='run_old'")
        ).first()
    # The legacy run recorded a workspace; the project columns are derived from it rather than left
    # NULL, so an old run still resolves to *a* project instead of silently matching every project.
    assert row[0], "project_root was not backfilled from the legacy workspace"


def test_migrations_are_idempotent(tmp_path: Path):
    db_path = tmp_path / "old.db"
    _v1_database(db_path)
    Database.open(db_path)
    before = _columns(db_path, "runs")
    # Re-opening (and re-running the runner) must be a no-op, not a duplicate-column error.
    Database.open(db_path)
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        run_migrations(engine, db_path=db_path)
        assert current_version(conn) == LATEST_VERSION
    engine.dispose()
    assert _columns(db_path, "runs") == before


def test_a_backup_is_taken_before_upgrading(tmp_path: Path):
    db_path = tmp_path / "old.db"
    _v1_database(db_path)
    Database.open(db_path)
    backups = list(tmp_path.glob("old.db.v1.*.bak"))
    assert backups, "no pre-migration backup was written"
    # The backup is the *old* database: it must still have the pre-migration shape.
    assert "project_id" not in _columns(backups[0], "runs")


def test_migration_report_exposes_backup_and_verification(tmp_path: Path):
    db_path = tmp_path / "old.db"
    _v1_database(db_path)
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    report = run_migrations(engine, db_path=db_path)
    engine.dispose()
    assert report.backup_path is not None and report.backup_path.exists()
    assert report.integrity_check == "ok"
    assert report.foreign_key_violations == ()
    assert report.row_counts["runs"] == 1
    assert report.applied == tuple(f"{version:04d}" for version in range(2, LATEST_VERSION + 1))


def test_fresh_database_needs_no_backup(tmp_path: Path):
    """A brand-new DB has nothing to lose; do not litter the data dir with empty backups."""

    Database.open(tmp_path / "fresh.db")
    assert not list(tmp_path.glob("*.bak"))


def test_a_future_schema_is_refused_rather_than_opened(tmp_path: Path):
    """Fail closed: old code must not write against a schema a newer release created (§15)."""

    db_path = tmp_path / "future.db"
    Database.open(db_path)
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE schema_meta SET value=:v WHERE key='version'"),
            {"v": str(LATEST_VERSION + 5)},
        )
    engine.dispose()

    with pytest.raises(SchemaTooNewError) as excinfo:
        Database.open(db_path)
    assert str(LATEST_VERSION + 5) in str(excinfo.value)
    assert "upgrade" in str(excinfo.value).lower()


def test_an_unknown_revision_is_refused_fail_closed(tmp_path: Path):
    db_path = tmp_path / "unknown.db"
    Database.open(db_path)
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO schema_meta (key, value) VALUES ('revision', 'deadbeef') "
                "ON CONFLICT(key) DO UPDATE SET value='deadbeef'"
            )
        )
    engine.dispose()
    with pytest.raises(UnknownRevisionError, match="unknown database revision"):
        Database.open(db_path)


def test_an_interrupted_migration_leaves_the_version_behind_and_retries_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A migration that dies mid-way must roll back, not half-apply and claim success."""

    db_path = tmp_path / "old.db"
    _v1_database(db_path)

    real_apply = MIGRATIONS[-1].apply
    calls = {"n": 0}

    def exploding_apply(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            real_apply(conn)
            raise RuntimeError("power cut mid-migration")
        return real_apply(conn)

    monkeypatch.setattr(MIGRATIONS[-1], "apply", exploding_apply)
    with pytest.raises(RuntimeError, match="power cut"):
        Database.open(db_path)

    # The version must NOT claim the failed migration landed.
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        assert current_version(conn) < LATEST_VERSION
    engine.dispose()

    # Retrying without the fault completes cleanly.
    monkeypatch.setattr(MIGRATIONS[-1], "apply", real_apply)
    Database.open(db_path)
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        assert current_version(conn) == LATEST_VERSION
    engine.dispose()


def test_in_memory_database_migrates_to_latest():
    db = Database.in_memory()
    with db.engine.begin() as conn:
        assert current_version(conn) == LATEST_VERSION
        assert current_revision(conn) == f"{LATEST_VERSION:04d}"
        assert {row[1] for row in conn.execute(text("PRAGMA table_info(events)"))} >= {"body"}
        assert conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='event_sequences'")
        ).first()
