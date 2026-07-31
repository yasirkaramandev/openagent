"""Migrations 0015–0017 (spec §23, §23.4).

These revisions are not in the active chain on this branch — 0014 is on the 0.1.6 release branches and
has not reached ``main``. That is a deliberate hold, and the first test class pins *why*, so nobody
"fixes" it by splicing a chain with a hole in it onto a user's database.

Everything else here exercises the real migration bodies against a real SQLite database built by the
real base chain, plus a fixture that stands in for what 0014 adds. So the bodies are verified now and
the eventual registration is a one-line decision rather than a piece of unproven work.

The properties that matter most are the ones a schema test usually skips: that a backfill invents
nothing, that a legacy boolean is not promoted to a live probe, that row identity survives, and that a
malformed row blocks the upgrade loudly instead of being silently dropped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from openagent.storage.migrations import (
    LATEST_REVISION,
    LATEST_VERSION,
    MIGRATIONS,
    Migration,
    MigrationVerificationError,
)
from openagent.storage.migrations_v2 import (
    BASE_REVISION,
    LEGACY_SOURCE,
    V2_MIGRATIONS,
    _m0015_provider_protocol_and_region,
    _m0016_capability_evidence,
    _m0017_session_resume,
    base_revision_present,
    register_v2_migrations,
    registration_status,
)

# =================================================================== the chain is registered


class TestTheChainIsRegistered:
    """The hold is over: 0014 shipped in 0.1.6, so 0015-0017 are part of the real chain.

    This class used to assert the opposite — that 0015-0017 were deliberately *not* spliced —
    because appending them onto a chain ending at 0013 would leave a hole that ``run_migrations``
    walks into on a user's database. That reasoning was correct and is why the tests existed; it
    simply no longer applies. They are inverted rather than deleted so the chain cannot quietly
    come apart again.
    """

    def test_the_base_revision_is_in_the_chain(self) -> None:
        assert base_revision_present() is True, (
            "0014 has left the chain — 0015-0017 chain onto it and must not be registered without it"
        )

    def test_they_are_registered(self) -> None:
        registered = {migration.revision for migration in MIGRATIONS}
        assert {"0015", "0016", "0017"} <= registered

    def test_the_chain_has_no_hole_and_no_duplicate(self) -> None:
        """The invariant the hold was protecting, now asserted directly on the real chain."""

        revisions = [migration.revision for migration in MIGRATIONS]
        assert len(revisions) == len(set(revisions)), "a revision is registered twice"
        for previous, current in zip(MIGRATIONS, MIGRATIONS[1:], strict=False):
            assert current.down_revision == previous.revision, (
                f"hole: {current.revision} declares parent {current.down_revision!r}, "
                f"but the previous link is {previous.revision!r}"
            )

    def test_latest_revision_is_derived_from_the_finished_chain(self) -> None:
        """The concrete defect of the old shape.

        ``LATEST_REVISION`` was computed from ``MIGRATIONS[-1]`` at import, so a helper that
        appended to the list afterwards left it at 0014 — and ``Database.open()`` would then never
        apply 0015-0017 at all, silently, on a database it reported as up to date.
        """

        assert LATEST_REVISION == MIGRATIONS[-1].revision
        assert LATEST_REVISION == "0017"
        assert LATEST_VERSION == 17

    def test_registering_is_refused_while_the_base_is_missing(self) -> None:
        """The guard still works, for any future fragment that chains onto a missing revision."""

        chain: list[Migration] = [
            m for m in MIGRATIONS if m.revision not in {"0014", "0015", "0016", "0017"}
        ]
        assert register_v2_migrations(chain) is False
        assert not {m.revision for m in chain} & {"0015", "0016", "0017"}

    def test_registering_twice_does_not_duplicate(self) -> None:
        chain: list[Migration] = list(MIGRATIONS)
        register_v2_migrations(chain)
        assert [m.revision for m in chain].count("0015") == 1

    def test_the_fragment_is_internally_chained(self) -> None:
        assert [m.revision for m in V2_MIGRATIONS] == ["0015", "0016", "0017"]
        assert V2_MIGRATIONS[0].down_revision == BASE_REVISION
        assert V2_MIGRATIONS[1].down_revision == "0015"
        assert V2_MIGRATIONS[2].down_revision == "0016"

    def test_the_status_reports_active_to_doctor(self) -> None:
        status = registration_status()
        assert status["registered"] is True
        assert status["base_revision_present"] is True


# =========================================================================== fixtures


def _base_database(path: Path) -> None:
    """A database at the head of this build's real chain, plus what 0014 adds.

    The 0014 stand-in is a *fixture*, not a copy of the migration: it creates the column 0014 adds so
    that 0015 runs against the shape it will really see. Duplicating 0014's body in production code
    would fork the chain; duplicating its observable effect in a test does not.
    """

    from openagent.storage.migrations import run_migrations

    engine = create_engine(f"sqlite:///{path}", future=True)
    run_migrations(engine, db_path=path)
    with engine.begin() as conn:
        columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(provider_connections)")
        }
        if "generation" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE provider_connections ADD COLUMN generation INTEGER NOT NULL DEFAULT 0"
            )
        conn.exec_driver_sql("UPDATE schema_meta SET value='0014' WHERE key='revision'")
    engine.dispose()


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "openagent.db"
    _base_database(path)
    engine = create_engine(f"sqlite:///{path}", future=True)
    yield engine
    engine.dispose()


def _insert_provider(conn, provider_id: str, provider_type: str, data: dict) -> None:
    """Insert a complete provider row.

    Every NOT NULL column is supplied. A helper that omits one fails on the constraint rather than on
    the thing under test, which makes the whole file read as broken migrations.
    """

    conn.execute(
        text(
            "INSERT INTO provider_connections "
            "(id, name, normalized_name, provider_type, enabled, state_revision, updated_at, data) "
            "VALUES (:id, :name, :normalized, :type, 1, 0, '', :data)"
        ),
        {
            "id": provider_id,
            "name": provider_id,
            "normalized": provider_id.lower(),
            "type": provider_type,
            "data": json.dumps({"id": provider_id, "name": provider_id, **data}),
        },
    )


def _insert_run(conn, run_id: str, **columns: object) -> None:
    """Insert a complete run row, with any extra columns the caller needs."""

    fields = {
        "id": run_id,
        "agent": "a",
        "status": "completed",
        "workspace": "/tmp/ws",
        "started_at": "2026-01-01T00:00:00+00:00",
        "execution_backend": "local",
        "state_revision": 0,
        "data": json.dumps({"id": run_id, "agent": "a", "status": "completed"}),
    }
    fields.update(columns)
    names = ", ".join(fields)
    binds = ", ".join(f":{name}" for name in fields)
    conn.execute(text(f"INSERT INTO runs ({names}) VALUES ({binds})"), fields)


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


# =========================================================================== 0015


class TestMigration0015:
    def test_it_adds_the_columns_the_provider_list_filters_on(self, db) -> None:
        with db.begin() as conn:
            _m0015_provider_protocol_and_region(conn)
            columns = _columns(conn, "provider_connections")
        assert {
            "protocol",
            "model_discovery",
            "region",
            "workspace_id",
            "server_state_enabled",
            "is_local",
            "profile_version",
        } <= columns

    def test_it_backfills_from_the_rows_own_json_and_invents_nothing(self, db) -> None:
        with db.begin() as conn:
            _insert_provider(
                conn,
                "p1",
                "kimi",
                {
                    "id": "p1",
                    "name": "p1",
                    "provider_type": "kimi",
                    "protocol": "anthropic-messages",
                    "region": "cn",
                    "workspace_id": "ws-9",
                },
            )
            _m0015_provider_protocol_and_region(conn)
            row = (
                conn.exec_driver_sql(
                    "SELECT protocol, region, workspace_id, model_discovery FROM provider_connections "
                    "WHERE id='p1'"
                )
                .mappings()
                .one()
            )
        assert row["protocol"] == "anthropic-messages"
        assert row["region"] == "cn"
        assert row["workspace_id"] == "ws-9"
        assert row["model_discovery"] == "openai-models"

    def test_discovery_strategy_follows_the_provider_type(self, db) -> None:
        with db.begin() as conn:
            for provider_id, provider_type in (
                ("g", "gemini"),
                ("o", "openrouter"),
                ("ol", "ollama"),
                ("lm", "lmstudio"),
                ("q", "qwen"),
                ("d", "deepseek"),
            ):
                _insert_provider(conn, provider_id, provider_type, {"provider_type": provider_type})
            _m0015_provider_protocol_and_region(conn)
            found = {
                row["id"]: row["model_discovery"]
                for row in conn.exec_driver_sql(
                    "SELECT id, model_discovery FROM provider_connections"
                ).mappings()
            }
        assert found["g"] == "gemini-models"
        assert found["o"] == "openrouter-catalog"
        assert found["ol"] == "ollama-tags-show"
        assert found["lm"] == "lmstudio-native"
        assert found["q"] == "curated-catalog"
        assert found["d"] == "openai-models"

    def test_server_state_is_never_inferred_as_enabled(self, db) -> None:
        """A row written before the column existed did not have it on, whatever the provider allows."""

        with db.begin() as conn:
            _insert_provider(conn, "p1", "gemini", {"provider_type": "gemini"})
            _m0015_provider_protocol_and_region(conn)
            value = conn.exec_driver_sql(
                "SELECT server_state_enabled FROM provider_connections WHERE id='p1'"
            ).scalar()
        assert value == 0

    def test_an_explicit_server_state_preference_survives(self, db) -> None:
        with db.begin() as conn:
            _insert_provider(
                conn, "p1", "gemini", {"provider_type": "gemini", "server_state_enabled": True}
            )
            _m0015_provider_protocol_and_region(conn)
            value = conn.exec_driver_sql(
                "SELECT server_state_enabled FROM provider_connections WHERE id='p1'"
            ).scalar()
        assert value == 1

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("http://localhost:11434", 1),
            ("http://127.0.0.1:1234/v1", 1),
            ("https://api.deepseek.com", 0),
            # The substring trap: this contains "localhost" and is not loopback.
            ("https://localhost.attacker.example/v1", 0),
        ],
    )
    def test_locality_is_decided_by_the_parsed_host(self, db, url: str, expected: int) -> None:
        with db.begin() as conn:
            _insert_provider(conn, "p1", "ollama", {"provider_type": "ollama", "base_url": url})
            _m0015_provider_protocol_and_region(conn)
            value = conn.exec_driver_sql(
                "SELECT is_local FROM provider_connections WHERE id='p1'"
            ).scalar()
        assert value == expected

    def test_an_unimplementable_protocol_blocks_the_upgrade_loudly(self, db) -> None:
        """Never silently dropped: only the user knows what that row was meant to be."""

        with db.begin() as conn:
            _insert_provider(
                conn, "p1", "custom", {"provider_type": "custom", "protocol": "grpc-something"}
            )
            with pytest.raises(MigrationVerificationError, match="wire protocol"):
                _m0015_provider_protocol_and_region(conn)

    def test_the_blocking_error_does_not_disclose_the_row_id(self, db) -> None:
        with db.begin() as conn:
            _insert_provider(
                conn,
                "secret-provider-name",
                "custom",
                {"provider_type": "custom", "protocol": "nope"},
            )
            with pytest.raises(MigrationVerificationError) as caught:
                _m0015_provider_protocol_and_region(conn)
        assert "secret-provider-name" not in str(caught.value)
        assert "sha256:" in str(caught.value)

    def test_it_is_idempotent(self, db) -> None:
        with db.begin() as conn:
            _insert_provider(conn, "p1", "deepseek", {"provider_type": "deepseek"})
            _m0015_provider_protocol_and_region(conn)
            _m0015_provider_protocol_and_region(conn)
            count = conn.exec_driver_sql("SELECT COUNT(*) FROM provider_connections").scalar()
        assert count == 1

    def test_row_count_and_identity_survive(self, db) -> None:
        with db.begin() as conn:
            for index in range(5):
                _insert_provider(conn, f"p{index}", "deepseek", {"provider_type": "deepseek"})
            before = {
                str(row[0]) for row in conn.exec_driver_sql("SELECT id FROM provider_connections")
            }
            _m0015_provider_protocol_and_region(conn)
            after = {
                str(row[0]) for row in conn.exec_driver_sql("SELECT id FROM provider_connections")
            }
        assert before == after
        assert len(after) == 5


# =========================================================================== 0016


class TestMigration0016:
    def _model_row(self, conn, provider_id: str, model_id: str, capabilities: dict) -> None:
        _insert_provider(conn, provider_id, "deepseek", {"provider_type": "deepseek"})
        conn.execute(
            text(
                "INSERT INTO models (id, provider_connection, remote_model_id, data) "
                "VALUES (:id, :provider, :remote, :data)"
            ),
            {
                "id": model_id,
                "provider": provider_id,
                "remote": "deepseek-v3",
                "data": json.dumps(
                    {
                        "id": model_id,
                        "provider_connection": provider_id,
                        "remote_model_id": "deepseek-v3",
                        "capabilities": capabilities,
                        "capabilities_tested_at": "2026-01-01T00:00:00+00:00",
                    }
                ),
            },
        )

    def test_it_creates_the_evidence_table(self, db) -> None:
        with db.begin() as conn:
            _m0016_capability_evidence(conn)
            tables = {
                str(row[0])
                for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert "capability_evidence" in tables

    def test_a_legacy_boolean_becomes_evidence_with_an_honest_source(self, db) -> None:
        """Not live_probe. That is the strongest automatic source, and this row proves nothing."""

        with db.begin() as conn:
            self._model_row(conn, "p1", "m1", {"text": True, "tool_calling": True})
            _m0016_capability_evidence(conn)
            rows = list(
                conn.exec_driver_sql(
                    "SELECT capability, status, source, probe_version FROM capability_evidence "
                    "ORDER BY capability"
                ).mappings()
            )
        assert [row["capability"] for row in rows] == ["text", "tool_calling"]
        assert all(row["source"] == LEGACY_SOURCE for row in rows)
        assert all(row["status"] == "supported" for row in rows)
        # probe_version 0: produced by no probe definition still in use, so freshness treats it stale.
        assert all(row["probe_version"] == 0 for row in rows)

    def test_a_false_legacy_flag_becomes_unsupported(self, db) -> None:
        with db.begin() as conn:
            self._model_row(conn, "p1", "m1", {"tool_calling": False})
            _m0016_capability_evidence(conn)
            status = conn.exec_driver_sql(
                "SELECT status FROM capability_evidence WHERE capability='tool_calling'"
            ).scalar()
        assert status == "unsupported"

    def test_a_null_legacy_flag_writes_no_row_at_all(self, db) -> None:
        """None means "never determined"; a row for it would record knowledge that does not exist."""

        with db.begin() as conn:
            self._model_row(conn, "p1", "m1", {"text": True, "tool_calling": None})
            _m0016_capability_evidence(conn)
            capabilities = {
                str(row[0])
                for row in conn.exec_driver_sql("SELECT capability FROM capability_evidence")
            }
        assert capabilities == {"text"}

    def test_vision_is_migrated_to_the_v2_capability_name(self, db) -> None:
        with db.begin() as conn:
            self._model_row(conn, "p1", "m1", {"vision": True})
            _m0016_capability_evidence(conn)
            capability = conn.exec_driver_sql("SELECT capability FROM capability_evidence").scalar()
        assert capability == "image_input"

    def test_evidence_is_keyed_by_the_remote_model_id(self, db) -> None:
        with db.begin() as conn:
            self._model_row(conn, "p1", "row-id-not-model-id", {"text": True})
            _m0016_capability_evidence(conn)
            model_id = conn.exec_driver_sql("SELECT model_id FROM capability_evidence").scalar()
        assert model_id == "deepseek-v3"

    def test_a_model_whose_provider_is_gone_is_skipped_not_fatal(self, db) -> None:
        """A dangling model row is a pre-existing inconsistency, not this upgrade's problem."""

        with db.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO models (id, provider_connection, remote_model_id, data) "
                    "VALUES ('m1', 'gone', 'x', :data)"
                ),
                {"data": json.dumps({"capabilities": {"text": True}})},
            )
            _m0016_capability_evidence(conn)
            count = conn.exec_driver_sql("SELECT COUNT(*) FROM capability_evidence").scalar()
        assert count == 0

    def test_it_is_idempotent(self, db) -> None:
        with db.begin() as conn:
            self._model_row(conn, "p1", "m1", {"text": True})
            _m0016_capability_evidence(conn)
            _m0016_capability_evidence(conn)
            count = conn.exec_driver_sql("SELECT COUNT(*) FROM capability_evidence").scalar()
        assert count == 1

    def test_deleting_a_provider_cascades_its_evidence(self, db) -> None:
        """Evidence about a model reached through a deleted credential is not evidence any more."""

        with db.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            self._model_row(conn, "p1", "m1", {"text": True})
            _m0016_capability_evidence(conn)
            conn.exec_driver_sql("DELETE FROM models WHERE provider_connection='p1'")
            conn.exec_driver_sql("DELETE FROM provider_connections WHERE id='p1'")
            count = conn.exec_driver_sql("SELECT COUNT(*) FROM capability_evidence").scalar()
        assert count == 0

    def test_identical_observations_collide_but_different_ones_do_not(self, db) -> None:
        """Deduplication is by explicit observation_key, not by a UNIQUE over the columns.

        A UNIQUE spanning the identity columns deduplicates nothing where it matters: SQLite never
        treats two NULLs as equal, and region, workspace_id and model_revision are nullable — so
        the rows most likely to repeat were exactly the ones that never collided, and re-running
        the backfill inserted each of them again.
        """

        from openagent.storage.migrations_v2 import observation_key

        def key(**overrides):
            base = {
                "provider_id": "p1",
                "model_id": "m",
                "capability": "tool_calling",
                "source": "live_probe",
                "protocol": "openai-chat",
                "base_url_fingerprint": "fp",
                "credential_revision": "revA",
                "region": None,
                "workspace_id": None,
                "probe_version": 1,
                "model_revision": None,
            }
            base.update(overrides)
            return observation_key(**base)

        with db.begin() as conn:
            _m0016_capability_evidence(conn)
            _insert_provider(conn, "p1", "deepseek", {"provider_type": "deepseek"})
            for observation in (
                key(),  # first observation
                key(),  # byte-identical repeat -> collides
                key(source="provider_catalog"),  # a different source is a different fact
                key(credential_revision="revB"),  # a rotated credential is a different fact
                key(region="eu"),  # NULL vs "eu" must still be two rows
            ):
                conn.execute(
                    text(
                        "INSERT OR IGNORE INTO capability_evidence "
                        "(provider_id, model_id, capability, status, source, observed_at, "
                        " observation_key) "
                        "VALUES ('p1','m','tool_calling','supported','live_probe','2026-01-01',"
                        " :key)"
                    ),
                    {"key": observation},
                )
            count = conn.exec_driver_sql("SELECT COUNT(*) FROM capability_evidence").scalar()

        # Five inserts, one an exact repeat: four rows.
        assert count == 4


# =========================================================================== 0017


class TestMigration0017:
    def test_it_adds_every_column_resume_verification_compares(self, db) -> None:
        with db.begin() as conn:
            _m0017_session_resume(conn)
            columns = _columns(conn, "runs")
        assert {
            "resume_mode",
            "remote_session_id",
            "remote_interaction_id",
            "continuation_path",
            "continuation_hash",
            "continuation_schema",
            "runtime_version",
            "provider_fingerprint",
            "model_fingerprint",
            "project_fingerprint",
        } <= columns

    def test_the_default_resume_mode_is_the_weakest_one(self, db) -> None:
        """Always available, so it is the safe default; claiming a stronger one would be a promise."""

        with db.begin() as conn:
            _m0017_session_resume(conn)
            default = conn.exec_driver_sql(
                "SELECT dflt_value FROM pragma_table_info('runs') WHERE name='resume_mode'"
            ).scalar()
        assert "normalized_replay" in str(default)

    def test_a_run_with_a_cli_session_id_is_backfilled_as_a_native_session(self, db) -> None:
        with db.begin() as conn:
            columns = _columns(conn, "runs")
            if "session_id" not in columns:
                pytest.skip("this build's runs table has no session id column to backfill from")
            _insert_run(conn, "r1", session_id="sess-abc")
            _m0017_session_resume(conn)
            row = (
                conn.exec_driver_sql(
                    "SELECT resume_mode, remote_session_id FROM runs WHERE id='r1'"
                )
                .mappings()
                .one()
            )
        assert row["resume_mode"] == "native_session"
        assert row["remote_session_id"] == "sess-abc"

    def test_a_run_without_a_session_id_keeps_the_default(self, db) -> None:
        with db.begin() as conn:
            _insert_run(conn, "r2")
            _m0017_session_resume(conn)
            mode = conn.exec_driver_sql("SELECT resume_mode FROM runs WHERE id='r2'").scalar()
        assert mode == "normalized_replay"

    def test_an_unknown_resume_mode_blocks_the_upgrade(self, db) -> None:
        with db.begin() as conn:
            _m0017_session_resume(conn)
            _insert_run(conn, "r3", resume_mode="teleport")
            with pytest.raises(MigrationVerificationError, match="resume mode"):
                _m0017_session_resume(conn)

    def test_it_is_idempotent(self, db) -> None:
        with db.begin() as conn:
            _insert_run(conn, "r1")
            _m0017_session_resume(conn)
            _m0017_session_resume(conn)
            count = conn.exec_driver_sql("SELECT COUNT(*) FROM runs").scalar()
        assert count == 1

    def test_it_stores_a_reference_not_the_payload(self, db) -> None:
        """Keeping the bytes in the row is what makes listing sessions slow (spec §23.3)."""

        with db.begin() as conn:
            _m0017_session_resume(conn)
            columns = _columns(conn, "runs")
        assert "continuation_path" in columns
        assert "continuation_hash" in columns
        assert "continuation_payload" not in columns
        assert "continuation" not in columns


# =========================================================================== the whole chain


class TestTheFragmentAppliesInOrder:
    def test_all_three_apply_cleanly_on_a_real_database(self, db) -> None:
        with db.begin() as conn:
            _insert_provider(
                conn,
                "p1",
                "gemini",
                {"provider_type": "gemini", "protocol": "gemini-interactions", "region": "global"},
            )
            _insert_run(conn, "r1")
            for migration in V2_MIGRATIONS:
                migration.upgrade(conn)

        with db.connect() as conn:
            integrity = conn.exec_driver_sql("PRAGMA integrity_check").scalar()
            violations = list(conn.exec_driver_sql("PRAGMA foreign_key_check"))
            provider = (
                conn.exec_driver_sql(
                    "SELECT protocol, model_discovery, region FROM provider_connections WHERE id='p1'"
                )
                .mappings()
                .one()
            )
            run = (
                conn.exec_driver_sql("SELECT resume_mode FROM runs WHERE id='r1'").mappings().one()
            )

        assert integrity == "ok"
        assert violations == []
        assert provider["protocol"] == "gemini-interactions"
        assert provider["model_discovery"] == "gemini-models"
        assert provider["region"] == "global"
        assert run["resume_mode"] == "normalized_replay"

    def test_a_failure_mid_chain_rolls_the_transaction_back(self, db) -> None:
        """0015 blocking must leave the database as it was, not half-migrated."""

        with db.begin() as conn:
            _insert_provider(conn, "ok", "deepseek", {"provider_type": "deepseek"})
        try:
            with db.begin() as conn:
                _insert_provider(
                    conn, "bad", "custom", {"provider_type": "custom", "protocol": "nope"}
                )
                for migration in V2_MIGRATIONS:
                    migration.upgrade(conn)
        except MigrationVerificationError:
            pass
        else:  # pragma: no cover - the fixture guarantees the raise
            pytest.fail("the malformed row should have blocked the upgrade")

        with db.connect() as conn:
            ids = {
                str(row[0]) for row in conn.exec_driver_sql("SELECT id FROM provider_connections")
            }
            integrity = conn.exec_driver_sql("PRAGMA integrity_check").scalar()
        assert ids == {"ok"}, "the rolled-back transaction left the bad row behind"
        assert integrity == "ok"

    def test_reapplying_the_whole_fragment_is_safe(self, db) -> None:
        with db.begin() as conn:
            _insert_provider(conn, "p1", "deepseek", {"provider_type": "deepseek"})
            for migration in V2_MIGRATIONS:
                migration.upgrade(conn)
            for migration in V2_MIGRATIONS:
                migration.upgrade(conn)
        with db.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM provider_connections").scalar() == 1


class TestProviderStorageParity:
    """Relational columns and the JSON aggregate are two encodings of one fact (spec §6.4).

    They used to disagree: 0015 wrote the columns and nothing wrote them into ``data``, because
    ProviderConnection was ``extra="forbid"`` and declared none of the fields. The domain model now
    declares them, so the migration projects both and the repository refuses a row where they
    differ.

    Silently preferring one side is the outcome worth preventing. The domain model is rebuilt from
    ``data`` while queries filter on the columns, so "which connections are local" and "is this
    connection local" could answer differently for the same row, and which answer a caller got
    would depend on the code path it happened to take.
    """

    def test_a_created_provider_agrees_with_itself(self) -> None:
        from openagent.core.models import DiscoveryStrategy, Protocol, ProviderConnection
        from openagent.storage.db import Database
        from openagent.storage.repositories import Repositories

        db = Database.in_memory()
        repos = Repositories(db)
        repos.providers.create(
            ProviderConnection(
                id="p1",
                name="Parity",
                provider_type="openai",
                protocol=Protocol.OPENAI_CHAT,
                model_discovery=DiscoveryStrategy.OPENAI_MODELS,
                region="us",
                server_state_enabled=True,
            )
        )
        # Reading through the parity decoder is the assertion: it raises if they disagree.
        loaded = repos.providers.get("p1")
        assert loaded is not None
        assert loaded.region == "us"
        assert loaded.server_state_enabled is True
        assert repos.providers.list()[0].id == "p1"

    def test_a_tampered_column_is_refused_rather_than_silently_preferred(self) -> None:
        from openagent.core.models import ProviderConnection
        from openagent.storage.db import Database
        from openagent.storage.repositories import ProviderStorageMismatch, Repositories

        db = Database.in_memory()
        repos = Repositories(db)
        repos.providers.create(
            ProviderConnection(id="p2", name="Tampered", provider_type="openai", region="us")
        )
        with db.engine.begin() as conn:
            conn.exec_driver_sql("UPDATE provider_connections SET region='eu' WHERE id='p2'")

        with pytest.raises(ProviderStorageMismatch) as caught:
            repos.providers.get("p2")
        assert "region" in caught.value.fields
        # The record is named; the values never are — they include base URLs and headers.
        assert "eu" not in str(caught.value)
        assert "us" not in str(caught.value)

    def test_is_local_is_derived_from_the_url_not_settable(self) -> None:
        """0.0.0.0 is not loopback, and no caller can assert that it is."""

        from openagent.core.models import ProviderConnection

        assert ProviderConnection(
            id="a", name="A", provider_type="ollama", base_url="http://127.0.0.1:11434"
        ).is_local
        assert ProviderConnection(
            id="b", name="B", provider_type="ollama", base_url="http://[::1]:11434"
        ).is_local
        assert ProviderConnection(
            id="c", name="C", provider_type="ollama", base_url="http://127.0.0.2:11434"
        ).is_local
        for remote in ("http://0.0.0.0:11434", "http://192.168.1.20:11434", "http://example.com"):
            assert not ProviderConnection(
                id="r", name="R", provider_type="ollama", base_url=remote
            ).is_local, f"{remote} must not be treated as local"

    def test_is_local_cannot_be_supplied_as_a_field(self) -> None:
        from pydantic import ValidationError

        from openagent.core.models import ProviderConnection

        with pytest.raises(ValidationError):
            ProviderConnection(
                id="x",
                name="X",
                provider_type="ollama",
                base_url="http://example.com",
                is_local=True,
            )


class TestMigration0017Backfill:
    """0017 reads the session column that actually exists (spec §9.4).

    It looked for ``session_id`` and ``cli_session_id``. Neither has ever been a column on
    ``runs`` — the real one is ``provider_session_id`` — so the backfill matched nothing and every
    run was left ``normalized_replay`` with an empty ``remote_session_id``. Measured against a real
    database: 19 of 33 runs carried a session id and none were migrated, silently downgrading 19
    resumable sessions to replay-only.
    """

    def test_a_cli_run_becomes_a_native_session(self, db) -> None:
        with db.begin() as conn:
            _seed_run_with_session(conn, run_id="r1", agent="cli-agent", runtime="cli")
            _m0017_session_resume(conn)
            row = conn.exec_driver_sql(
                "SELECT resume_mode, remote_session_id FROM runs WHERE id='r1'"
            ).first()
        assert row[0] == "native_session"
        assert row[1] == "sess-abc"

    def test_an_api_run_keeps_the_id_without_claiming_a_resumable_mode(self, db) -> None:
        """A session id does not say which *kind* of session it is.

        A CLI id names a session the CLI can reattach to. An API id names provider-side state that
        only means anything if the provider was asked to keep it — which this migration cannot
        verify retroactively — so it is recorded as a remote interaction and the mode stays
        conservative.
        """

        with db.begin() as conn:
            _seed_run_with_session(conn, run_id="r2", agent="api-agent", runtime="api-agent")
            _m0017_session_resume(conn)
            row = conn.exec_driver_sql(
                "SELECT resume_mode, remote_session_id, remote_interaction_id "
                "FROM runs WHERE id='r2'"
            ).first()
        assert row[0] == "normalized_replay"
        assert not row[1]
        assert row[2] == "sess-abc"

    def test_a_run_whose_agent_is_gone_is_left_alone(self, db) -> None:
        """With the agent deleted there is nothing to say what the id means, so nothing is claimed."""

        with db.begin() as conn:
            _seed_run_with_session(conn, run_id="r3", agent="deleted-agent", runtime=None)
            _m0017_session_resume(conn)
            row = conn.exec_driver_sql(
                "SELECT resume_mode, remote_session_id FROM runs WHERE id='r3'"
            ).first()
        assert row[0] == "normalized_replay"
        assert not row[1]


def _seed_run_with_session(conn, *, run_id: str, agent: str, runtime: str | None) -> None:
    """One run carrying a legacy ``provider_session_id``, optionally with its agent present."""

    if runtime is not None:
        provider_id = None
        if runtime == "api-agent":
            # ck_agents_api_provider: an API agent without a provider is the "agent exists,
            # provider missing" state the schema exists to prevent, so the fixture cannot skip it.
            provider_id = f"prov-{agent}"
            _insert_provider(conn, provider_id, "openai", {"provider_type": "openai"})
        conn.execute(
            text(
                "INSERT INTO agents (name, normalized_name, title, runtime_type, provider_id, "
                "state_revision, data) VALUES (:n, :n, '', :rt, :pid, 0, '{}')"
            ),
            {"n": agent, "rt": runtime, "pid": provider_id},
        )
    conn.execute(
        text(
            "INSERT INTO runs (id, agent, status, workspace, provider_session_id, started_at, "
            "execution_backend, state_revision, data) "
            "VALUES (:id, :agent, 'completed', '/tmp', 'sess-abc', '2026-01-01T00:00:00Z', "
            "'local', 0, '{}')"
        ),
        {"id": run_id, "agent": agent},
    )
