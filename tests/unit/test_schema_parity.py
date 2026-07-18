"""A fresh database and an upgraded one must end up with the same schema (spec §10).

The two paths do genuinely different things. A fresh install runs ``_m001``, which calls
``metadata.create_all`` with **today's** table definitions — so it gets every constraint the model
declares, including ``runs.project_id REFERENCES projects(id)``. An existing install ran that same
migration against an *older* metadata (no ``project_id`` at all), and later revisions added the
column with ``ALTER TABLE runs ADD COLUMN``.

SQLite's ``ALTER TABLE ADD COLUMN`` cannot attach a foreign key. So the constraint silently exists
on new machines and silently does not on upgraded ones, and nothing in the test suite noticed
because every test started from a fresh database. Two users on the same version get different
integrity guarantees, and only one of them finds out.

These tests build both databases for real and compare them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from openagent.storage.db import Database, _configure_sqlite
from openagent.storage.migrations import LATEST_REVISION, run_migrations

#: The ``runs`` table as it existed before revision 0002 — i.e. what a v0.1.0/v0.1.1 install has on
#: disk. Written out literally rather than derived, because the point of the test is to reproduce a
#: real old database rather than whatever today's model would produce.
_LEGACY_RUNS_DDL = """
CREATE TABLE runs (
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
    data JSON NOT NULL,
    PRIMARY KEY (id)
)
"""

DOMAIN_TABLES = (
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


def _engine(path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{path}", future=True)
    _configure_sqlite(engine)
    return engine


def _fresh(path: Path) -> Engine:
    Database.open(path)
    return _engine(path)


def _upgraded_from_legacy(path: Path) -> Engine:
    """A database that looks like an old install, then brought up to date.

    Revision 0001 is applied first so every *other* table is realistic, then ``runs`` alone is
    rewound to its pre-0002 shape and the revision stamp is reset. That isolates the question this
    file is about — what happens to ``runs`` when later revisions add its columns by ``ALTER TABLE``
    — instead of also re-litigating how v0.1.0 happened to spell unrelated tables.
    """

    from openagent.storage.migrations import _m001_base_schema

    engine = _engine(path)
    with engine.begin() as conn:
        _m001_base_schema(conn)
        conn.exec_driver_sql("DROP TABLE runs")
        conn.exec_driver_sql(_LEGACY_RUNS_DDL)
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_meta "
            "(key VARCHAR NOT NULL PRIMARY KEY, value VARCHAR NOT NULL)"
        )
        conn.exec_driver_sql("DELETE FROM schema_meta WHERE key IN ('revision','version')")
        conn.exec_driver_sql("INSERT INTO schema_meta (key, value) VALUES ('revision', '0001')")
        conn.exec_driver_sql("INSERT INTO schema_meta (key, value) VALUES ('version', '1')")
        # A real run, so the migration has data to carry across.
        conn.exec_driver_sql(
            "INSERT INTO runs (id, agent, status, workspace, started_at, data) VALUES "
            "('run_legacy', 'codex', 'completed', '/tmp/proj', "
            "'2026-01-01T00:00:00+00:00', ?)",
            (
                json.dumps(
                    {
                        "id": "run_legacy",
                        "agent": "codex",
                        "status": "completed",
                        "workspace": "/tmp/proj",
                        "started_at": "2026-01-01T00:00:00+00:00",
                    }
                ),
            ),
        )
    run_migrations(engine, db_path=path)
    return engine


def _pragma(engine: Engine, sql: str) -> list[tuple]:
    with engine.connect() as conn:
        return [tuple(row) for row in conn.exec_driver_sql(sql).all()]


def _columns(engine: Engine, table: str) -> dict[str, str]:
    """Column name → declared type, ignoring ordering."""

    return {row[1]: str(row[2]).upper() for row in _pragma(engine, f"PRAGMA table_info({table})")}


def _foreign_keys(engine: Engine, table: str) -> set[tuple[str, str, str]]:
    """(referenced table, from column, to column) for each FK, order-independent."""

    return {
        (str(row[2]), str(row[3]), str(row[4]))
        for row in _pragma(engine, f"PRAGMA foreign_key_list({table})")
    }


def _indexes(engine: Engine, table: str) -> set[str]:
    # Auto-indexes are named by SQLite and carry no design intent.
    return {
        str(row[1])
        for row in _pragma(engine, f"PRAGMA index_list({table})")
        if not str(row[1]).startswith("sqlite_autoindex")
    }


@pytest.fixture()
def both(tmp_path: Path) -> tuple[Engine, Engine]:
    return _fresh(tmp_path / "fresh.db"), _upgraded_from_legacy(tmp_path / "upgraded.db")


def test_both_paths_reach_the_latest_revision(both: tuple[Engine, Engine]) -> None:
    for engine in both:
        with engine.connect() as conn:
            revision = conn.exec_driver_sql(
                "SELECT value FROM schema_meta WHERE key='revision'"
            ).first()
        assert revision is not None and revision[0] == LATEST_REVISION


def test_runs_has_the_project_foreign_key_after_upgrade(both: tuple[Engine, Engine]) -> None:
    """The headline: an upgraded database must enforce the same referential integrity."""

    fresh, upgraded = both
    expected = ("projects", "project_id", "id")
    assert expected in _foreign_keys(fresh, "runs"), "the fresh schema lost its FK"
    assert expected in _foreign_keys(upgraded, "runs"), (
        "an upgraded database has no runs.project_id -> projects.id foreign key, so the same "
        "OpenAgent version enforces different integrity depending on when you installed it"
    )


@pytest.mark.parametrize("table", DOMAIN_TABLES)
def test_columns_match_between_fresh_and_upgraded(both: tuple[Engine, Engine], table: str) -> None:
    fresh, upgraded = both
    assert _columns(fresh, table) == _columns(upgraded, table)


@pytest.mark.parametrize("table", DOMAIN_TABLES)
def test_indexes_match_between_fresh_and_upgraded(both: tuple[Engine, Engine], table: str) -> None:
    fresh, upgraded = both
    assert _indexes(fresh, table) == _indexes(upgraded, table)


@pytest.mark.parametrize("table", DOMAIN_TABLES)
def test_foreign_keys_match_between_fresh_and_upgraded(
    both: tuple[Engine, Engine], table: str
) -> None:
    fresh, upgraded = both
    assert _foreign_keys(fresh, table) == _foreign_keys(upgraded, table)


def _schema_objects(engine: Engine) -> dict[tuple[str, str, str], str]:
    rows = _pragma(
        engine,
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' AND type IN ('table', 'index')",
    )
    return {
        (str(type_), str(name), str(table)): " ".join(str(sql or "").split())
        for type_, name, table, sql in rows
    }


def test_sqlite_schema_objects_have_fresh_upgrade_parity(both: tuple[Engine, Engine]) -> None:
    """Exercise ``sqlite_schema.sql`` in addition to PRAGMA's semantic schema views.

    SQLite can spell the equivalent foreign key inline or as a table constraint, so raw table DDL
    text is not a stable semantic equality check. Object identity must match, and every user table
    plus explicit index must have inspectable SQL; PRAGMA tests above compare the actual semantics.
    """

    fresh, upgraded = both
    fresh_objects = _schema_objects(fresh)
    upgraded_objects = _schema_objects(upgraded)
    assert set(fresh_objects) == set(upgraded_objects)
    assert all(sql for sql in fresh_objects.values())
    assert all(sql for sql in upgraded_objects.values())


def test_upgrade_preserves_the_existing_run(both: tuple[Engine, Engine]) -> None:
    """Schema parity must not be bought with data loss."""

    _fresh_engine, upgraded = both
    with upgraded.connect() as conn:
        rows = conn.exec_driver_sql("SELECT id, agent, status FROM runs").all()
    assert [tuple(row) for row in rows] == [("run_legacy", "codex", "completed")]


def test_upgraded_database_passes_integrity_checks(both: tuple[Engine, Engine]) -> None:
    _fresh_engine, upgraded = both
    with upgraded.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA integrity_check").first()[0] == "ok"
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
