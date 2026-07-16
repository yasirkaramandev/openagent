from __future__ import annotations

from pathlib import Path

from ..core.models import Project
from ..storage.projects import read_project_marker, write_project_marker


class ProjectError(RuntimeError):
    pass


class ProjectService:
    def __init__(self, app) -> None:
        self.app = app

    def list(self) -> list[Project]:
        projects: list[Project] = []
        for project in self.app.repos.projects.list():
            root = Path(project.root)
            marker = read_project_marker(root)
            state = (
                "missing"
                if not root.exists()
                else "moved_or_marker_mismatch"
                if marker is None or marker.id != project.id
                else "active"
            )
            if state != project.state:
                project = project.model_copy(update={"state": state})
                self.app.repos.projects.upsert(project)
            projects.append(project)
        return projects

    def relocate(self, project_id: str, new_root: Path) -> Project:
        root = new_root.resolve()
        if not root.is_dir():
            raise ProjectError(f"new project root does not exist: {root}")
        marker = read_project_marker(root)
        if marker is None:
            raise ProjectError(f"{root} has no valid .openagent/project.json marker")
        if marker.id != project_id:
            raise ProjectError(
                f"marker id {marker.id} does not match requested project {project_id}"
            )
        try:
            project = self.app.repos.projects.relocate(project_id, root)
        except (KeyError, ValueError) as exc:
            raise ProjectError(str(exc)) from exc
        write_project_marker(project)
        return project
