"""Application container.

Wires paths, DB, repositories, and the credential store, and exposes the service layer. TUI, CLI,
and MCP all go through this single object so business logic lives in one place (spec §36).
"""

from __future__ import annotations

from pathlib import Path

from .config import KEYCHAIN_SERVICE, Paths, ensure_dirs, get_paths
from .credentials.store import CredentialStore
from .storage.db import Database
from .storage.projects import ensure_project_marker, write_project_marker
from .storage.repositories import Repositories


class OpenAgentApp:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        ensure_dirs(paths)
        project = ensure_project_marker(paths.project_root)
        self.db = Database.open(paths.db_path)
        self.repos = Repositories(self.db)
        # An upgraded DB may already know this root under its legacy path-derived id. Preserve that
        # identity and rewrite the new marker once, rather than creating a duplicate project row.
        registered = self.repos.projects.get_by_root(project.root)
        if registered is not None and registered.id != project.id:
            project = registered.model_copy(update={"marker_version": 1, "state": "active"})
            write_project_marker(project)
        else:
            self.repos.projects.upsert(project)
        self.project = project
        self.credentials = CredentialStore(KEYCHAIN_SERVICE)
        self._services: dict[str, object] = {}

    @classmethod
    def create(cls, project_root: Path | None = None) -> OpenAgentApp:
        return cls(get_paths(project_root))

    # -- lazy service accessors ------------------------------------------------

    @property
    def providers(self):
        from .services.provider_service import ProviderService

        return self._cached("providers", lambda: ProviderService(self))

    @property
    def models(self):
        from .services.model_service import ModelService

        return self._cached("models", lambda: ModelService(self))

    @property
    def agents(self):
        from .services.agent_service import AgentService

        return self._cached("agents", lambda: AgentService(self))

    @property
    def runs(self):
        from .services.run_service import RunService

        return self._cached("runs", lambda: RunService(self))

    @property
    def clis(self):
        from .services.discovery_service import DiscoveryService

        return self._cached("clis", lambda: DiscoveryService(self))

    @property
    def doctor(self):
        from .services.doctor_service import DoctorService

        return self._cached("doctor", lambda: DoctorService(self))

    @property
    def projects(self):
        from .services.project_service import ProjectService

        return self._cached("projects", lambda: ProjectService(self))

    def _cached(self, key: str, factory):
        if key not in self._services:
            self._services[key] = factory()
        return self._services[key]
