"""Application container.

Wires paths, DB, repositories, and the credential store, and exposes the service layer. TUI, CLI,
and MCP all go through this single object so business logic lives in one place (spec §36).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .config import KEYCHAIN_SERVICE, Paths, ensure_dirs, get_paths
from .credentials.store import CredentialStore
from .security.journal import OperationJournal
from .storage.db import Database
from .storage.projects import ensure_project_marker, write_project_marker
from .storage.repositories import Repositories

if TYPE_CHECKING:
    from .runtimes.cli.update_policy import (
        PostUpdateFailureCallback,
        UpdatePromptCallback,
    )


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
        self.journal = OperationJournal(paths.journal_dir)
        self._recover_operations()
        #: How to ask the user about an available CLI update under ``CliUpdatePolicy.ASK``.
        #:
        #: ``None`` means no interactive surface is attached — a piped shell, CI, a cron job — and
        #: ASK degrades to NOTIFY rather than blocking on input that will never arrive. A TUI or an
        #: interactive CLI sets this; nothing else needs to.
        self.update_prompt: UpdatePromptCallback | None = None
        self.update_failure_prompt: PostUpdateFailureCallback | None = None
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

    def _recover_operations(self) -> None:
        """Finish/compensate operations interrupted after a durable journal write."""

        from .core.models import CredentialRef
        from .reporting.openagent_md import OpenAgentMdConflict, write_openagent_md

        for operation in self.journal.pending():
            # Another OpenAgent process may be paused between its durable DB write and projection
            # or compensation. PID + process start time makes that ownership resistant to PID reuse.
            # Missing identity belongs to a legacy entry and retains the historical recovery path.
            if operation.is_owned_by_live_other_process():
                continue
            if operation.kind in {"provider_add", "provider_remove"}:
                from .services.provider_service import (
                    PROVIDER_COMMIT_PROVEN_STAGES,
                    PROVIDER_ROLLBACK_PROVEN_STAGES,
                    STAGE_LEGACY_CLEANUP_PENDING,
                    STAGE_RECOVERY_AMBIGUOUS,
                    STAGE_SUPERSEDED_GENERATION,
                )

                provider_id = str(operation.payload.get("provider_id") or "")
                expected_revision = operation.payload.get("credential_revision")
                if not isinstance(expected_revision, str) or not expected_revision.strip():
                    # Legacy journal entries did not carry ownership. We may clean an orphaned,
                    # revision-scoped secret when no row exists, but must never guess ownership of
                    # a live row.
                    expected_revision = ""
                current_revision = (
                    self.repos.providers.credential_revision_of(provider_id)
                    if provider_id
                    else None
                )
                raw_ref = operation.payload.get("credential")
                legacy_ref = operation.payload.get("legacy_credential")
                try:
                    credential = (
                        CredentialRef.model_validate(raw_ref) if isinstance(raw_ref, dict) else None
                    )
                    if operation.kind == "provider_add":
                        stage = operation.stage
                        if stage in PROVIDER_COMMIT_PROVEN_STAGES:
                            # Durably committed. The row and its new revision-scoped credential are
                            # authoritative and must never be deleted; only the non-atomic
                            # legacy-secret cleanup may still need finishing (the leftover is the
                            # user's OLD pre-revision secret).
                            operation.advance(STAGE_LEGACY_CLEANUP_PENDING)
                            if isinstance(legacy_ref, dict):
                                self.credentials.delete_secret(
                                    CredentialRef.model_validate(legacy_ref), strict=True
                                )
                            operation.complete()
                            continue
                        if stage in PROVIDER_ROLLBACK_PROVEN_STAGES:
                            # The transaction entered its rollback path: compensate only this
                            # operation's own generation, and only while the live row still is it.
                            if current_revision == expected_revision and expected_revision:
                                self.repos.providers.delete_owned_with_probes(
                                    provider_id, expected_revision
                                )
                                current_revision = None
                            elif (
                                current_revision is not None
                                and current_revision != expected_revision
                            ):
                                operation.advance(STAGE_SUPERSEDED_GENERATION)
                            if current_revision is None and credential is not None:
                                self.credentials.delete_secret(credential, strict=True)
                            operation.complete()
                            continue
                        # Ambiguous stage: a bare ``db_written`` (an interrupted commit or a crash
                        # before __exit__ ran) or an unknown/legacy stage. We cannot prove the
                        # transaction rolled back, so we must never delete a row that may be
                        # committed. Fail-safe: preserve on any doubt.
                        if current_revision is None:
                            # No row: the add never became durable, or was already compensated.
                            # Only this operation's own orphaned secret is safe to clean.
                            if credential is not None:
                                self.credentials.delete_secret(credential, strict=True)
                            operation.complete()
                            continue
                        if expected_revision and current_revision != expected_revision:
                            # A newer generation already owns this id — our operation is superseded.
                            # Never touch the live newer row; clean only our own scoped secret.
                            operation.advance(STAGE_SUPERSEDED_GENERATION)
                            if credential is not None:
                                self.credentials.delete_secret(credential, strict=True)
                            operation.complete()
                            continue
                        # current_revision == expected_revision (or an unverifiable legacy entry
                        # pointing at a live row): a committed provider and a crashed-before-commit
                        # add are indistinguishable here. Preserve the row AND its credential; mark
                        # the operation for Doctor instead of guessing. The legacy secret is left in
                        # place too — deleting it could destroy the user's still-valid old key.
                        operation.advance(STAGE_RECOVERY_AMBIGUOUS)
                        continue

                    # provider_remove: unchanged — the row's absence proves the remove committed.
                    if current_revision is None or (
                        expected_revision and current_revision != expected_revision
                    ):
                        if current_revision is not None:
                            operation.advance(STAGE_SUPERSEDED_GENERATION)
                        if credential is not None:
                            self.credentials.delete_secret(credential, strict=True)
                    if current_revision == expected_revision and isinstance(legacy_ref, dict):
                        self.credentials.delete_secret(
                            CredentialRef.model_validate(legacy_ref), strict=True
                        )
                    # A legacy entry that points at a live row has no ownership proof. Keep it
                    # pending for Doctor instead of silently declaring compensation complete.
                    if current_revision is not None and not expected_revision:
                        continue
                    operation.complete()
                except Exception:
                    # Keychain/DB compensation can be retried. Startup stays available and Doctor
                    # reports the durable pending operation; no raw credential payload is rendered.
                    continue
            elif operation.kind == "agent_document_sync":
                path = Path(str(operation.payload["path"]))
                try:
                    write_openagent_md(path, self.repos.agents.list)
                except OpenAgentMdConflict:
                    # A document the user must fix by hand must not make OpenAgent unstartable —
                    # the interface that can fix it is the thing being blocked. The journal entry
                    # is left pending so the sync is retried once the conflict is resolved, and
                    # doctor reports it in the meantime.
                    continue
                operation.complete()
