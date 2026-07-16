"""Stable project UUID marker and relocation identity helpers."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from ..core.models import Project
from ..security.atomic import atomic_write_text
from ..security.filesystem import UnsafeWorkspacePath

PROJECT_MARKER_VERSION = 1


def canonical_root(project_root: Path) -> Path:
    try:
        return project_root.resolve()
    except (OSError, RuntimeError):
        return project_root.absolute()


def legacy_project_id_for(project_root: Path) -> str:
    digest = hashlib.sha256(str(canonical_root(project_root)).encode("utf-8")).hexdigest()
    return f"proj_{digest[:16]}"


def marker_path(project_root: Path) -> Path:
    return canonical_root(project_root) / ".openagent" / "project.json"


def read_project_marker(project_root: Path) -> Project | None:
    path = marker_path(project_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        project_id = str(uuid.UUID(str(payload["id"])))
        version = int(payload["version"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if version != PROJECT_MARKER_VERSION:
        return None
    return Project(id=project_id, root=str(canonical_root(project_root)), marker_version=version)


def write_project_marker(project: Project) -> None:
    root = canonical_root(Path(project.root))
    state_dir = root / ".openagent"
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        info = state_dir.lstat()
        if state_dir.is_symlink() or not state_dir.is_dir():
            raise UnsafeWorkspacePath(f"project state path is not a real directory: {state_dir}")
        del info
    except OSError as exc:
        raise UnsafeWorkspacePath(f"cannot verify project state directory: {state_dir}") from exc
    atomic_write_text(
        state_dir / "project.json",
        json.dumps(
            {
                "version": PROJECT_MARKER_VERSION,
                "id": project.id,
                "root": str(root),
            },
            indent=2,
        ),
        mode=0o600,
    )


def ensure_project_marker(project_root: Path) -> Project:
    existing = read_project_marker(project_root)
    if existing is not None:
        return existing
    project = Project(id=str(uuid.uuid4()), root=str(canonical_root(project_root)))
    write_project_marker(project)
    return project


def project_id_for(project_root: Path) -> str:
    marker = read_project_marker(project_root)
    return marker.id if marker else legacy_project_id_for(project_root)
