"""Migration 0014 makes provider credential generation relational and authoritative."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, text

from openagent.core.models import ProviderConnection
from openagent.storage.db import Database
from openagent.storage.migrations import LATEST_REVISION


def _revision(path: Path) -> str:
    engine = create_engine(f"sqlite:///{path}", future=True)
    try:
        with engine.connect() as conn:
            return str(
                conn.execute(
                    text("SELECT value FROM schema_meta WHERE key='revision'")
                ).scalar_one()
            )
    finally:
        engine.dispose()


def test_0014_backfills_credential_revision_without_changing_provider_identity(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "provider-0013.db"
    Database.open(db_path).engine.dispose()
    provider = ProviderConnection(
        id="provider_acme",
        name="acme",
        provider_type="custom",
        base_url="https://api.example.invalid/v1",
        extra_headers={"X-Safe-Metadata": "kept"},
        credential_revision="generation-a",
    )

    raw = sqlite3.connect(db_path)
    try:
        raw.execute(
            "INSERT INTO provider_connections "
            "(id, name, normalized_name, provider_type, enabled, state_revision, updated_at, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                provider.id,
                provider.name,
                provider.name,
                provider.provider_type,
                1,
                0,
                "2026-01-01T00:00:00+00:00",
                provider.model_dump_json(),
            ),
        )
        raw.execute("UPDATE schema_meta SET value='0013' WHERE key='revision'")
        raw.execute("UPDATE schema_meta SET value='13' WHERE key='version'")
        raw.commit()
    finally:
        raw.close()

    before_ids = {"provider_acme"}
    Database.open(db_path).engine.dispose()
    assert LATEST_REVISION == "0014"
    assert _revision(db_path) == "0014"

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    try:
        with engine.connect() as conn:
            columns = {
                str(row[1])
                for row in conn.exec_driver_sql("PRAGMA table_info(provider_connections)")
            }
            row = conn.execute(
                text(
                    "SELECT id, credential_revision, data "
                    "FROM provider_connections WHERE id='provider_acme'"
                )
            ).one()
            assert columns >= {"credential_revision"}
            assert {str(row[0])} == before_ids
            assert row[1] == "generation-a"
            decoded = ProviderConnection.model_validate(
                json.loads(row[2]) if isinstance(row[2], str) else row[2]
            )
            assert decoded.credential_revision == row[1]
            assert decoded.base_url == provider.base_url
            assert decoded.extra_headers == provider.extra_headers
            assert conn.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
