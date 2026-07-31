"""Shared pytest fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from openagent.config import Paths, ensure_dirs
from openagent.runtimes.cli.registry import unregister_cli_adapter
from openagent.storage.db import Database
from openagent.storage.repositories import Repositories

try:
    import keyring
    from keyring.backend import KeyringBackend

    class _MemoryKeyring(KeyringBackend):
        """An in-memory keyring so tests never touch the real OS keychain and behave the same on
        headless CI (which has no backend)."""

        priority = 1  # type: ignore[assignment]

        def __init__(self) -> None:
            super().__init__()
            self._store: dict[tuple[str, str], str] = {}

        def get_password(self, service: str, username: str) -> str | None:
            return self._store.get((service, username))

        def set_password(self, service: str, username: str, password: str) -> None:
            self._store[(service, username)] = password

        def delete_password(self, service: str, username: str) -> None:
            self._store.pop((service, username), None)
except Exception:  # pragma: no cover - keyring always present as a dependency
    keyring = None  # type: ignore[assignment]
    _MemoryKeyring = None  # type: ignore[assignment,misc]


@pytest.fixture(autouse=True)
def _memory_keyring() -> None:
    """Use an isolated in-memory keyring for every test."""
    if keyring is not None and _MemoryKeyring is not None:
        keyring.set_keyring(_MemoryKeyring())


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
    # HOME too, not just the OpenAgent dirs. Foreign tools keep state under the home directory —
    # Antigravity's updater lock is the one that bit us — so a suite that leaves HOME alone reads
    # the developer's real files and passes or fails according to what is on their machine. CI has
    # a clean home and stayed green while a workstation with a stale lock failed.
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    os.environ.pop("OPENAI_API_KEY", None)
    # Antigravity's editing bypass is opt-in; a stray env var on the dev machine must not silently
    # change what the tests are asserting (item 15).
    os.environ.pop("OPENAGENT_ANTIGRAVITY_EXPERIMENTAL_EDIT", None)
    os.environ.pop("OPENAGENT_ANTIGRAVITY_DANGEROUS_BYPASS", None)


@pytest.fixture(autouse=True)
def _clean_cli_registry() -> Iterator[None]:
    """The CLI registry is process-global; drop any fake adapter a test installed into it."""
    yield
    unregister_cli_adapter("fake")
