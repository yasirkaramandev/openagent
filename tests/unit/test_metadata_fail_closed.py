"""Compatibility metadata that cannot be parsed must never be treated as satisfied."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from openagent.core.errors import DatabaseMetadataValidationError
from openagent.storage.db import Database


def _set_meta(path: Path, **values: str) -> None:
    engine = create_engine(f"sqlite:///{path}", future=True)
    try:
        with engine.begin() as conn:
            for key, value in values.items():
                conn.execute(
                    text(
                        "INSERT INTO schema_meta (key, value) VALUES (:key, :value) "
                        "ON CONFLICT(key) DO UPDATE SET value=:value"
                    ),
                    {"key": key, "value": value},
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize("key", ["minimum_reader_version", "last_writer_version"])
def test_malformed_version_metadata_blocks_without_echoing_raw_value(
    tmp_path: Path, key: str
) -> None:
    path = tmp_path / "metadata.db"
    Database.open(path).engine.dispose()
    raw = "not-a-version-prefixless-sensitive-fragment"
    _set_meta(path, **{key: raw})

    with pytest.raises(DatabaseMetadataValidationError) as excinfo:
        Database.open(path)

    error = excinfo.value
    assert error.metadata_key == key
    assert raw not in str(error)
    assert "openagent doctor --json" in str(error)
    assert "openagent update --repair" in str(error)


def test_schema_revision_version_mismatch_is_typed_metadata_failure(tmp_path: Path) -> None:
    path = tmp_path / "metadata.db"
    Database.open(path).engine.dispose()
    _set_meta(path, version="12")

    with pytest.raises(DatabaseMetadataValidationError) as excinfo:
        Database.open(path)

    assert excinfo.value.metadata_key == "revision/version"
    assert "12" not in str(excinfo.value)
