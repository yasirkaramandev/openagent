"""Filesystem locations and global settings.

Two scopes (spec §34):

* **Global** user-data dir (platformdirs) holds the single SQLite DB with providers, models,
  capabilities, credential references, CLI installations, agents, runs, and sessions.
* **Per-project** ``.openagent/`` holds run artifacts and the append-only ``events.jsonl`` — the
  human-inspectable source of truth for events. SQLite keeps only an index.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import platformdirs

APP_NAME = "openagent"
KEYCHAIN_SERVICE = "openagent"

#: Marker comments used to delimit the auto-generated agent block in OPENAGENT.md (spec §33).
OPENAGENT_MD_START = "<!-- OPENAGENT:AGENTS:START -->"
OPENAGENT_MD_END = "<!-- OPENAGENT:AGENTS:END -->"


@dataclass(frozen=True)
class Paths:
    """Resolved on-disk locations for a given working directory."""

    data_dir: Path
    config_dir: Path
    db_path: Path
    project_root: Path

    @property
    def project_state_dir(self) -> Path:
        """``.openagent/`` inside the current project."""
        return self.project_root / ".openagent"

    @property
    def runs_dir(self) -> Path:
        return self.project_state_dir / "runs"

    @property
    def worktrees_dir(self) -> Path:
        return self.project_state_dir / "worktrees"

    @property
    def runtime_dir(self) -> Path:
        return self.project_state_dir / "runtime"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def openagent_md(self) -> Path:
        return self.project_root / "OPENAGENT.md"


def _env_override(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


def get_paths(project_root: Path | None = None) -> Paths:
    """Resolve global + project paths.

    Honors ``OPENAGENT_DATA_DIR`` / ``OPENAGENT_CONFIG_DIR`` (used by tests to sandbox state).
    """

    data_dir = _env_override(
        "OPENAGENT_DATA_DIR", Path(platformdirs.user_data_dir(APP_NAME))
    )
    config_dir = _env_override(
        "OPENAGENT_CONFIG_DIR", Path(platformdirs.user_config_dir(APP_NAME))
    )
    root = (project_root or Path.cwd()).resolve()
    return Paths(
        data_dir=data_dir,
        config_dir=config_dir,
        db_path=data_dir / "openagent.db",
        project_root=root,
    )


def ensure_dirs(paths: Paths) -> None:
    """Create the directories OpenAgent writes to (idempotent)."""

    for directory in (
        paths.data_dir,
        paths.config_dir,
        paths.project_state_dir,
        paths.runs_dir,
        paths.worktrees_dir,
        paths.runtime_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
