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

from openagent.core.models import (
    AgentProfile,
    AgentRuntime,
    ModelProfile,
    ProviderConnection,
    RuntimeType,
)
from openagent.storage.db import Database
from openagent.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    MigrationFailedError,
    MigrationVerificationError,
    SchemaTooNewError,
    UnknownRevisionError,
    current_revision,
    current_version,
    run_migrations,
)
from openagent.storage.repositories import Repositories


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
    assert set(report.row_counts) >= {
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
    }
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
    with pytest.raises(MigrationFailedError) as excinfo:
        Database.open(db_path)
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "power cut" in str(excinfo.value.__cause__)

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


def _rewind_to(db: Database, revision: str) -> None:
    with db.engine.begin() as conn:
        conn.execute(
            text("UPDATE schema_meta SET value=:revision WHERE key='revision'"),
            {"revision": revision},
        )
        conn.execute(
            text("UPDATE schema_meta SET value=:version WHERE key='version'"),
            {"version": str(int(revision))},
        )


def test_legacy_nvidia_normalization_preserves_bindings_and_invalidates_probe(tmp_path: Path):
    db_path = tmp_path / "nvidia.db"
    db = Database.open(db_path)
    repos = Repositories(db)
    provider = ProviderConnection(
        id="provider_legacy",
        name="legacy-name",
        provider_type="openai",
        protocol="openai-chat",
        base_url="HTTPS://INTEGRATE.API.NVIDIA.COM:443/v1/",
        credential_revision="credential-revision-kept",
    )
    repos.providers.upsert(provider)
    repos.models.upsert(
        ModelProfile(
            id="model_kept",
            provider_connection=provider.id,
            remote_model_id="nvidia/model-kept",
        )
    )
    repos.agents.upsert(
        AgentProfile(
            name="agent-kept",
            runtime=AgentRuntime(
                type=RuntimeType.API_AGENT,
                provider=provider.id,
                model="model_kept",
            ),
        )
    )
    repos.model_probes.put(
        cache_key="probe-kept",
        provider_id=provider.id,
        model_id="nvidia/model-kept",
        base_url_fingerprint="endpoint-fingerprint",
        protocol="openai-chat",
        credential_revision="credential-revision-kept",
        probe_version="1",
        tested_at="2026-01-01T00:00:00+00:00",
        data={
            "model": "nvidia/model-kept",
            "text": True,
            "streaming": True,
            "tool_calling": True,
            "agent_compatible": True,
            "category": "verified",
            "detail": "",
            "tested_at": "2026-01-01T00:00:00+00:00",
            "probe_version": "1",
        },
    )
    _rewind_to(db, "0009")
    db.engine.dispose()

    upgraded = Database.open(db_path)
    upgraded_repos = Repositories(upgraded)
    normalized = upgraded_repos.providers.get(provider.id)
    assert normalized is not None
    assert normalized.name == provider.name
    assert normalized.provider_type == "nvidia-build"
    assert normalized.credential == provider.credential
    assert normalized.credential_revision == provider.credential_revision
    assert upgraded_repos.models.get("model_kept").remote_model_id == "nvidia/model-kept"
    assert upgraded_repos.agents.get("agent-kept").runtime.provider == provider.id
    with upgraded.engine.connect() as conn:
        probe = conn.execute(
            text("SELECT probe_version, data FROM model_probes WHERE cache_key='probe-kept'")
        ).first()
    assert probe is not None
    assert probe[0] == "invalidated-provider-normalization"
    assert json.loads(probe[1])["probe_version"] == "invalidated-provider-normalization"


@pytest.mark.parametrize(
    ("provider_type", "base_url"),
    [
        ("custom", "https://integrate.api.nvidia.com/v1"),
        ("openai", "https://integrate.api.nvidia.com/v1?gateway=custom"),
        ("openai", "https://integrate.api.nvidia.com/v1/extra"),
        ("openai", "https://integrate.api.nvidia.com:8443/v1"),
    ],
)
def test_nvidia_normalization_does_not_guess_custom_endpoints(
    tmp_path: Path, provider_type: str, base_url: str
):
    db_path = tmp_path / "custom.db"
    db = Database.open(db_path)
    provider = ProviderConnection(
        id="provider_custom",
        name="custom",
        provider_type=provider_type,
        protocol="openai-chat",
        base_url=base_url,
    )
    Repositories(db).providers.upsert(provider)
    _rewind_to(db, "0009")
    db.engine.dispose()

    upgraded = Database.open(db_path)
    assert Repositories(upgraded).providers.get(provider.id).provider_type == provider_type


def test_domain_validation_rolls_back_entire_chain_and_keeps_backup(tmp_path: Path):
    db_path = tmp_path / "corrupt.db"
    db = Database.open(db_path)
    repos = Repositories(db)
    repos.providers.upsert(
        ProviderConnection(
            id="provider_should_not_leak",
            name="legacy",
            provider_type="openai",
            protocol="openai-chat",
            base_url="https://integrate.api.nvidia.com/v1",
        )
    )
    secret = "secret-value-must-not-leak"
    with db.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO provider_connections (id, name, provider_type, enabled, data) "
            "VALUES (?, ?, ?, 1, ?)",
            ("provider_corrupt_identifier", "broken", "custom", json.dumps({"secret": secret})),
        )
    _rewind_to(db, "0009")
    db.engine.dispose()

    with pytest.raises(MigrationFailedError) as excinfo:
        Database.open(db_path)
    message = str(excinfo.value)
    assert "provider_connections" in message
    assert "sha256:" in message
    assert "provider_corrupt_identifier" not in message
    assert secret not in message
    assert list(tmp_path.glob("corrupt.db.v9.*.bak"))

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.connect() as conn:
        # Revision 0010 and its NVIDIA rewrite were in the same transaction as validation.
        assert current_revision(conn) == "0009"
        rows = conn.execute(
            text("SELECT id, provider_type FROM provider_connections ORDER BY id")
        ).all()
    engine.dispose()
    assert rows == [
        ("provider_corrupt_identifier", "custom"),
        ("provider_should_not_leak", "openai"),
    ]


def test_latest_database_with_corrupt_json_fails_closed_on_reopen(tmp_path: Path):
    db_path = tmp_path / "latest-corrupt.db"
    db = Database.open(db_path)
    with db.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO cli_installations (id, type, executable, data) VALUES (?, ?, ?, ?)",
            ("cli_private_name", "codex", "/private/bin/codex", "{}"),
        )
    db.engine.dispose()

    with pytest.raises(MigrationVerificationError) as excinfo:
        Database.open(db_path)
    assert "cli_installations" in str(excinfo.value)
    assert "cli_private_name" not in str(excinfo.value)


# --------------------------------------------------------------------------- revision 0012


def _pre_0012_database(path: Path, providers, agents) -> None:
    """A database at revision 0011: the provider/agent tables as they were before v0.1.6.

    ``providers`` is ``[(id, name)]``; ``agents`` is ``[(name, runtime_type, provider_name)]`` where
    ``provider_name`` is None for a CLI agent.
    """

    engine = create_engine(f"sqlite:///{path}", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE provider_connections ("
                " id VARCHAR PRIMARY KEY, name VARCHAR UNIQUE NOT NULL,"
                " provider_type VARCHAR NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,"
                " data JSON NOT NULL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE agents ("
                " name VARCHAR PRIMARY KEY, title VARCHAR NOT NULL DEFAULT '',"
                " runtime_type VARCHAR NOT NULL, data JSON NOT NULL)"
            )
        )
        conn.execute(
            text("CREATE TABLE schema_meta (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)")
        )
        conn.execute(text("INSERT INTO schema_meta (key, value) VALUES ('revision', '0011')"))
        conn.execute(text("INSERT INTO schema_meta (key, value) VALUES ('version', '11')"))
        for provider_id, name in providers:
            payload = json.dumps(
                {
                    "id": provider_id,
                    "name": name,
                    "provider_type": "openai",
                    "credential": {"type": "none"},
                }
            )
            conn.execute(
                text(
                    "INSERT INTO provider_connections (id, name, provider_type, data)"
                    " VALUES (:id, :name, 'openai', :data)"
                ),
                {"id": provider_id, "name": name, "data": payload},
            )
        for name, runtime_type, provider_name in agents:
            runtime: dict = {"type": runtime_type}
            if runtime_type == "cli":
                runtime["cli"] = "codex"
            else:
                runtime["provider"] = provider_name
                runtime["model"] = "gpt-4"
            payload = json.dumps({"name": name, "title": name, "runtime": runtime})
            conn.execute(
                text(
                    "INSERT INTO agents (name, title, runtime_type, data)"
                    " VALUES (:name, :name, :rt, :data)"
                ),
                {"name": name, "rt": runtime_type, "data": payload},
            )
    engine.dispose()


def test_0012_adds_concurrency_columns_and_the_provider_foreign_key(tmp_path: Path):
    db_path = tmp_path / "upgrade.db"
    _pre_0012_database(db_path, [("prov_1", "OpenAI")], [("coder", "api-agent", "OpenAI")])

    Database.open(db_path)

    assert {"normalized_name", "state_revision", "updated_at"} <= _columns(
        db_path, "provider_connections"
    )
    assert {"normalized_name", "provider_id", "state_revision", "updated_at"} <= _columns(
        db_path, "agents"
    )
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        # PRAGMA foreign_key_list columns: id, seq, table, from, to, on_update, on_delete, match.
        fks = list(conn.execute(text("PRAGMA foreign_key_list(agents)")))
        assert any(row[2] == "provider_connections" and row[6] == "RESTRICT" for row in fks)
    engine.dispose()


def test_0012_backfills_the_agent_provider_binding(tmp_path: Path):
    db_path = tmp_path / "backfill.db"
    _pre_0012_database(
        db_path,
        [("prov_1", "OpenAI"), ("prov_2", "Anthropic")],
        [
            ("coder", "api-agent", "OpenAI"),
            ("writer", "api-agent", "Anthropic"),
            ("cli-agent", "cli", None),
        ],
    )

    Database.open(db_path)

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        bindings = {
            row[0]: row[1] for row in conn.execute(text("SELECT name, provider_id FROM agents"))
        }
    engine.dispose()
    assert bindings["coder"] == "prov_1"
    assert bindings["writer"] == "prov_2"
    assert bindings["cli-agent"] is None


def test_0012_normalizes_names_for_case_insensitive_uniqueness(tmp_path: Path):
    db_path = tmp_path / "normalize.db"
    _pre_0012_database(db_path, [("prov_1", "OpenAI")], [("Coder", "cli", None)])

    Database.open(db_path)

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        provider_norm = conn.execute(
            text("SELECT normalized_name FROM provider_connections WHERE id='prov_1'")
        ).scalar()
        agent_norm = conn.execute(
            text("SELECT normalized_name FROM agents WHERE name='Coder'")
        ).scalar()
    engine.dispose()
    assert provider_norm == "openai"
    assert agent_norm == "coder"


def test_0012_blocks_on_case_colliding_providers_without_data_loss(tmp_path: Path):
    """A duplicate that only differs by case must stop the upgrade, not be resolved by guessing."""

    db_path = tmp_path / "dup.db"
    _pre_0012_database(db_path, [("prov_1", "OpenAI"), ("prov_2", "openai")], [])

    with pytest.raises(MigrationFailedError) as excinfo:
        Database.open(db_path)
    assert "collide" in str(excinfo.value)

    # Records and revision untouched; the backup stands.
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        ids = {row[0] for row in conn.execute(text("SELECT id FROM provider_connections"))}
        revision = conn.execute(text("SELECT value FROM schema_meta WHERE key='revision'")).scalar()
    engine.dispose()
    assert ids == {"prov_1", "prov_2"}, "a colliding row was dropped"
    assert revision == "0011", "the schema was advanced despite the refusal"


def test_0012_blocks_on_case_colliding_agents(tmp_path: Path):
    db_path = tmp_path / "dup-agents.db"
    _pre_0012_database(db_path, [], [("Coder", "cli", None), ("coder", "cli", None)])

    with pytest.raises(MigrationFailedError) as excinfo:
        Database.open(db_path)
    assert "collide" in str(excinfo.value)


def test_0012_leaves_a_dangling_binding_null_rather_than_blocking(tmp_path: Path):
    """An agent naming a provider that does not exist is already broken; it must not block startup."""

    db_path = tmp_path / "orphan.db"
    _pre_0012_database(db_path, [("prov_1", "OpenAI")], [("orphan", "api-agent", "GoneProvider")])

    Database.open(db_path)  # must not raise

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        binding = conn.execute(text("SELECT provider_id FROM agents WHERE name='orphan'")).scalar()
    engine.dispose()
    assert binding is None


def test_0012_upgraded_schema_matches_a_fresh_database(tmp_path: Path):
    """Schema parity: an upgraded database and a fresh one must have identical provider/agent shapes."""

    upgraded = tmp_path / "upgraded.db"
    _pre_0012_database(upgraded, [("prov_1", "OpenAI")], [("coder", "cli", None)])
    Database.open(upgraded)

    fresh = tmp_path / "fresh.db"
    Database.open(fresh)

    for table in ("provider_connections", "agents"):
        assert _columns(upgraded, table) == _columns(fresh, table), f"{table} shape diverged"


def _meta(path: Path, key: str) -> str | None:
    engine = create_engine(f"sqlite:///{path}", future=True)
    try:
        with engine.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT value FROM schema_meta WHERE key=:k", {"k": key}
            ).first()
            return None if row is None else str(row[0])
    finally:
        engine.dispose()


def test_open_stamps_writer_and_reader_metadata(tmp_path: Path):
    """Every open records who wrote and the domain-reader floor it implies (§6)."""

    from openagent import __version__
    from openagent.storage.migrations import MINIMUM_READER_VERSION

    db_path = tmp_path / "stamp.db"
    Database.open(db_path)

    assert _meta(db_path, "last_writer_version") == __version__
    assert _meta(db_path, "minimum_reader_version") == MINIMUM_READER_VERSION


def test_old_binary_reading_a_newer_database_gets_typed_error_not_traceback(tmp_path: Path):
    """The failure the user hit: an older reader must be refused from metadata, before any model load.

    Simulated by recording a ``minimum_reader_version`` this binary cannot satisfy — exactly what a
    future build would stamp. The gate must raise the typed, actionable error rather than letting the
    open reach ``ProviderConnection.model_validate`` and blow up with a raw ValidationError (§6).
    """

    from openagent.core.errors import DatabaseReaderCompatibilityError

    db_path = tmp_path / "newer.db"
    Database.open(db_path)
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO schema_meta (key, value) VALUES "
                "('minimum_reader_version', '99.0.0'), ('last_writer_version', '99.0.0') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
        )
    engine.dispose()

    with pytest.raises(DatabaseReaderCompatibilityError) as excinfo:
        Database.open(db_path)
    error = excinfo.value
    assert error.minimum_reader_version == "99.0.0"
    assert error.database_writer_version == "99.0.0"
    assert error.repair_commands == ["openagent update --repair"]
    assert "cannot safely read it" in str(error)
    assert "openagent update --repair" in str(error)


def test_compat_gate_fires_before_any_domain_row_is_decoded(tmp_path: Path):
    """A too-new DB is refused even when a domain row is itself undecodable — metadata only (§6)."""

    from openagent.core.errors import DatabaseReaderCompatibilityError

    db_path = tmp_path / "poison.db"
    Database.open(db_path)
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        # A row that would raise a raw ValidationError if the gate did not run first.
        conn.execute(
            text(
                "INSERT INTO provider_connections "
                "(id, name, normalized_name, provider_type, enabled, state_revision, updated_at, data) "
                "VALUES ('p', 'p', 'p', 'openai', 1, 0, '', :d)"
            ),
            {"d": '{"totally": "not a provider"}'},
        )
        conn.execute(
            text(
                "INSERT INTO schema_meta (key, value) VALUES ('minimum_reader_version', '99.0.0') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
        )
    engine.dispose()

    with pytest.raises(DatabaseReaderCompatibilityError):
        Database.open(db_path)
