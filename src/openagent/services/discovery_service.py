"""CLI discovery (spec §32 ``openagent discover``)."""

from __future__ import annotations

import asyncio
import builtins
import json
import os
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..core.events import EventType
from ..core.models import (
    CliInstallation,
    CliUpdatePolicy,
    CliUpdateState,
    CliUpdateStatus,
    RuntimeType,
)
from ..credentials.redaction import redact_mapping
from ..runtimes.cli.registry import build_cli_adapter, discover_installed, known_cli_types
from ..runtimes.cli.updates import (
    CliUpdateConfig,
    UpdateExecutionResult,
    check_update,
    load_update_config,
    perform_update,
    save_update_config,
)
from ..security.file_lock import file_lock
from ..storage.event_log import write_all

if TYPE_CHECKING:
    from ..app import OpenAgentApp


class DiscoveryService:
    def __init__(self, app: OpenAgentApp) -> None:
        self.app = app
        self.repos = app.repos

    async def discover(self, persist: bool = True) -> builtins.list[CliInstallation]:
        """Detect installed CLIs and (optionally) record them."""

        detected = await discover_installed()
        augmented: builtins.list[CliInstallation] = []
        for install in detected:
            install = await self._augment_auth(install)
            cached = self.repos.clis.get(install.id)
            if cached is not None and self._same_installation(cached, install):
                install = install.model_copy(
                    update={
                        "update_status": cached.update_status,
                        "last_checked_at": cached.last_checked_at,
                    }
                )
            augmented.append(install)
            if persist:
                self.repos.clis.upsert(install)
        if persist:
            found_ids = {installation.id for installation in detected}
            for cli_type in known_cli_types():
                cli_id = f"cli_{cli_type}"
                if cli_id not in found_ids:
                    self.repos.clis.delete(cli_id)
        return augmented

    def list(self) -> Sequence[CliInstallation]:
        return self.repos.clis.list()

    def known_types(self) -> Sequence[str]:
        return known_cli_types()

    def update_config(self) -> CliUpdateConfig:
        return load_update_config(self.app.paths.config_dir)

    def save_update_config(self, config: CliUpdateConfig) -> None:
        save_update_config(self.app.paths.config_dir, config)

    async def check_updates(self, *, refresh: bool = False) -> builtins.list[CliInstallation]:
        """Return cached statuses, refreshing only when explicitly requested and policy allows it."""

        installations = await self.discover(persist=True)
        config = self.update_config()
        checked: builtins.list[CliInstallation] = []
        for installation in installations:
            status = installation.update_status
            should_refresh = refresh and config.policy is not CliUpdatePolicy.NEVER
            if should_refresh:
                status = await asyncio.to_thread(
                    check_update,
                    installation,
                    cache_hours=config.check_interval_hours,
                )
            elif status is None:
                status = self._unknown_status(
                    installation,
                    "not checked; use `openagent cli check --refresh`",
                )
            updated = installation.model_copy(
                update={
                    "update_status": status,
                    "last_checked_at": status.checked_at,
                }
            )
            self.repos.clis.upsert(updated)
            checked.append(updated)
        return checked

    async def update(
        self,
        cli_type: str,
        *,
        dry_run: bool = False,
        exclude_run_ids: Sequence[str] = (),
    ) -> UpdateExecutionResult:
        """Update one exact active CLI, fail-closed on provenance/conflicts/live runs."""

        if cli_type not in known_cli_types():
            raise KeyError(f"unknown CLI type {cli_type!r}")
        config = self.update_config()
        installations = {item.type: item for item in await self.discover(persist=True)}
        installation = installations.get(cli_type)
        if installation is None:
            raise RuntimeError(f"{cli_type} is not installed")
        if config.policy is CliUpdatePolicy.NEVER:
            status = installation.update_status or self._unknown_status(
                installation, "CLI update policy is never"
            )
            blocked = status.model_copy(
                update={
                    "state": CliUpdateState.BLOCKED,
                    "detail": "CLI update policy is never",
                }
            )
            return UpdateExecutionResult(status=blocked, detail=blocked.detail)

        status = await asyncio.to_thread(
            check_update,
            installation,
            cache_hours=config.check_interval_hours,
        )
        self._audit(EventType.CLI_UPDATE_STARTED, installation, status, dry_run=dry_run)
        result = await asyncio.to_thread(
            perform_update,
            installation,
            status,
            active_run_ids=[
                run_id
                for run_id in self.active_run_ids(cli_type)
                if run_id not in set(exclude_run_ids)
            ],
            dry_run=dry_run,
        )
        event_type = EventType.CLI_UPDATE_COMPLETED
        if result.status.restart_required:
            event_type = EventType.CLI_UPDATE_RESTART_REQUIRED
        elif result.status.state in {CliUpdateState.BLOCKED, CliUpdateState.CHECK_FAILED}:
            event_type = EventType.CLI_UPDATE_FAILED
        self._audit(event_type, installation, result.status, dry_run=dry_run)

        persisted = installation.model_copy(
            update={
                "update_status": result.status,
                "last_checked_at": result.status.checked_at,
            }
        )
        if result.ran and not dry_run and not result.status.restart_required:
            # Rediscover the active path after package-manager mutation. Never assume the binary
            # that existed before the update still wins PATH resolution afterwards.
            adapter = build_cli_adapter(cli_type)
            rediscovered = await adapter.detect()
            if rediscovered is not None:
                path_changed = rediscovered.executable != installation.executable
                detail = result.status.detail
                if path_changed:
                    detail += (
                        f"; warning: active executable changed from {installation.executable} "
                        f"to {rediscovered.executable}"
                    )
                state = result.status.state
                if rediscovered.install_source != installation.install_source:
                    state = CliUpdateState.BLOCKED
                    detail += (
                        f"; active install source changed from {installation.install_source.value} "
                        f"to {rediscovered.install_source.value}"
                    )
                if rediscovered.shadowed_executables:
                    state = CliUpdateState.BLOCKED
                    detail += "; independent shadowed executable remains after update"
                revised_status = result.status.model_copy(update={"detail": detail, "state": state})
                result = result.model_copy(update={"status": revised_status, "detail": detail})
                persisted = rediscovered.model_copy(
                    update={
                        "update_status": revised_status,
                        "last_checked_at": revised_status.checked_at,
                    }
                )
        self.repos.clis.upsert(persisted)
        return result

    async def update_all(self, *, dry_run: bool = False) -> dict[str, UpdateExecutionResult]:
        results: dict[str, UpdateExecutionResult] = {}
        installed = {item.type for item in await self.discover(persist=True)}
        for cli_type in known_cli_types():
            if cli_type in installed:
                results[cli_type] = await self.update(cli_type, dry_run=dry_run)
        return results

    def active_run_ids(self, cli_type: str) -> builtins.list[str]:
        active: builtins.list[str] = []
        for run in self.repos.runs.list_active(all_projects=True):
            agent = self.repos.agents.get(run.agent)
            if agent is None:
                continue
            runtime_type = getattr(agent.runtime.type, "value", agent.runtime.type)
            if runtime_type == RuntimeType.CLI.value and agent.runtime.cli == cli_type:
                active.append(run.id)
        return active

    async def _augment_auth(self, install: CliInstallation) -> CliInstallation:
        adapter = build_cli_adapter(install.type, install.executable)
        try:
            status = await adapter.inspect_auth()
            return install.model_copy(update={"authenticated": status.authenticated})
        except Exception:  # noqa: BLE001 - auth probing is best-effort
            return install

    @staticmethod
    def _same_installation(left: CliInstallation, right: CliInstallation) -> bool:
        return (
            left.version == right.version
            and left.install_source == right.install_source
            and left.executable == right.executable
            and left.resolved_executable == right.resolved_executable
            and left.shadowed_executables == right.shadowed_executables
        )

    @staticmethod
    def _unknown_status(installation: CliInstallation, detail: str) -> CliUpdateStatus:
        return CliUpdateStatus(
            current_version=installation.version,
            install_source=installation.install_source,
            active_executable=installation.executable,
            resolved_executable=installation.resolved_executable or installation.executable,
            shadowed_executables=list(installation.shadowed_executables),
            state=CliUpdateState.UNKNOWN,
            detail=detail,
        )

    def _audit(
        self,
        event_type: EventType,
        installation: CliInstallation,
        status: CliUpdateStatus,
        *,
        dry_run: bool,
    ) -> None:
        """Append a redacted machine-level updater audit event with inter-process locking."""

        path = self.app.paths.data_dir / "cli-update-audit.jsonl"
        lock = path.with_suffix(path.suffix + ".lock")
        payload = {
            "id": "evt_" + uuid.uuid4().hex[:16],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type.value,
            "source": "openagent",
            "data": redact_mapping(
                {
                    "cli_type": installation.type,
                    "active_executable": installation.executable,
                    "install_source": installation.install_source.value,
                    "current_version": status.current_version,
                    "latest_version": status.latest_version,
                    "state": status.state.value,
                    "dry_run": dry_run,
                    "detail": status.detail,
                }
            ),
        }
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(lock, timeout=10):
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                write_all(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
