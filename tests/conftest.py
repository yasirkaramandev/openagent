"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from openagent.config import Paths, ensure_dirs
from openagent.storage.db import Database
from openagent.storage.repositories import Repositories


@pytest.fixture()
def database() -> Database:
    return Database.in_memory()


@pytest.fixture()
def repos(database: Database) -> Repositories:
    return Repositories(database)


@pytest.fixture()
def paths(tmp_path: Path) -> Paths:
    data = tmp_path / "data"
    config = tmp_path / "config"
    project = tmp_path / "project"
    project.mkdir()
    p = Paths(data_dir=data, config_dir=config, db_path=data / "openagent.db", project_root=project)
    ensure_dirs(p)
    return p


@pytest.fixture(autouse=True)
def _sandbox_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep global state out of the real user dirs during tests."""
    monkeypatch.setenv("OPENAGENT_DATA_DIR", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("OPENAGENT_CONFIG_DIR", str(tmp_path / "xdg-config"))
    os.environ.pop("OPENAI_API_KEY", None)
