from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openagent.app import OpenAgentApp
from openagent.core.models import (
    CliInstallation,
    CliInstallSource,
    CliUpdateState,
    CliUpdateStatus,
)
from openagent.runtimes.cli.updates import UpdateExecutionResult
from openagent.services import discovery_service as discovery_module
from openagent.services.discovery_service import DiscoveryService


def _installation(path: Path, *, version: str = "1.0.0") -> CliInstallation:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("binary", encoding="utf-8")
    return CliInstallation(
        id="cli_codex",
        type="codex",
        executable=str(path),
        resolved_executable=str(path),
        version=version,
        install_source=CliInstallSource.NPM,
        adapter="codex-json",
        authenticated=True,
    )


def _status(installation: CliInstallation, *, latest: str = "1.1.0") -> CliUpdateStatus:
    now = datetime.now(timezone.utc)
    return CliUpdateStatus(
        current_version=installation.version,
        latest_version=latest,
        update_available=True,
        state=CliUpdateState.AVAILABLE,
        install_source=installation.install_source,
        active_executable=installation.executable,
        resolved_executable=installation.resolved_executable or installation.executable,
        check_method="npm-registry",
        update_method="npm-install-latest",
        checked_at=now,
        cache_expires_at=now + timedelta(hours=6),
    )


def test_discovery_preserves_matching_cached_update_status(paths, monkeypatch):
    app = OpenAgentApp(paths)
    service = DiscoveryService(app)
    detected = _installation(paths.data_dir / "codex")
    cached_status = _status(detected)
    app.repos.clis.upsert(
        detected.model_copy(
            update={"update_status": cached_status, "last_checked_at": cached_status.checked_at}
        )
    )

    async def fake_discover():
        return [detected]

    async def identity(self, installation):
        return installation

    monkeypatch.setattr(discovery_module, "discover_installed", fake_discover)
    monkeypatch.setattr(DiscoveryService, "_augment_auth", identity)

    result = asyncio.run(service.discover(persist=True))

    assert result[0].update_status == cached_status
    assert app.repos.clis.get("cli_codex").update_status == cached_status


def test_changed_active_binary_invalidates_cached_update_status(paths, monkeypatch):
    app = OpenAgentApp(paths)
    service = DiscoveryService(app)
    old = _installation(paths.data_dir / "old-codex")
    app.repos.clis.upsert(old.model_copy(update={"update_status": _status(old)}))
    changed = _installation(paths.data_dir / "new-codex")

    async def fake_discover():
        return [changed]

    async def identity(self, installation):
        return installation

    monkeypatch.setattr(discovery_module, "discover_installed", fake_discover)
    monkeypatch.setattr(DiscoveryService, "_augment_auth", identity)

    result = asyncio.run(service.discover())

    assert result[0].update_status is None


def test_check_updates_is_offline_until_refresh(paths, monkeypatch):
    app = OpenAgentApp(paths)
    service = DiscoveryService(app)
    detected = _installation(paths.data_dir / "codex")
    calls = 0

    async def fake_discover(*, persist=True):
        del persist
        return [detected]

    def fake_check(installation, *, cache_hours):
        nonlocal calls
        assert cache_hours == 6
        calls += 1
        return _status(installation)

    monkeypatch.setattr(service, "discover", fake_discover)
    monkeypatch.setattr(discovery_module, "check_update", fake_check)

    cached = asyncio.run(service.check_updates(refresh=False))
    assert calls == 0
    assert cached[0].update_status.state is CliUpdateState.UNKNOWN

    refreshed = asyncio.run(service.check_updates(refresh=True))
    assert calls == 1
    assert refreshed[0].update_status.state is CliUpdateState.AVAILABLE


def test_explicit_update_writes_started_and_terminal_audit_events(paths, monkeypatch):
    app = OpenAgentApp(paths)
    service = DiscoveryService(app)
    detected = _installation(paths.data_dir / "codex")
    status = _status(detected)

    async def fake_discover(*, persist=True):
        del persist
        return [detected]

    def fake_perform(installation, checked, **kwargs):
        assert installation is detected
        assert checked is status
        assert kwargs["active_run_ids"] == []
        assert kwargs["dry_run"] is True
        return UpdateExecutionResult(
            status=checked,
            command=["npm", "install", "-g", "@openai/codex@latest"],
            detail="dry-run",
        )

    monkeypatch.setattr(service, "discover", fake_discover)
    monkeypatch.setattr(discovery_module, "check_update", lambda *args, **kwargs: status)
    monkeypatch.setattr(discovery_module, "perform_update", fake_perform)
    monkeypatch.setattr(service, "active_run_ids", lambda _cli: [])

    result = asyncio.run(service.update("codex", dry_run=True))

    assert result.detail == "dry-run"
    audit = paths.data_dir / "cli-update-audit.jsonl"
    events = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert [event["type"] for event in events] == [
        "cli.update.started",
        "cli.update.completed",
    ]
    assert all(event["data"]["cli_type"] == "codex" for event in events)
