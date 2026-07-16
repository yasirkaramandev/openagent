from __future__ import annotations

from pathlib import Path

import keyring
import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import (
    AgentProfile,
    AgentRuntime,
    CredentialRef,
    CredentialType,
    RuntimeType,
)
from openagent.credentials.store import CredentialError


def _paths(tmp_path: Path) -> Paths:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return Paths(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db",
        project_root=project,
    )


def test_startup_compensates_interrupted_provider_secret_write(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    app = OpenAgentApp(paths)
    credential = CredentialRef(
        type=CredentialType.KEYCHAIN,
        service="openagent",
        account="provider/interrupted/revision",
    )
    app.credentials.set_secret(credential, "prefixless-interrupted-secret")
    operation = app.journal.begin(
        "provider_add",
        {"provider_id": "provider_interrupted", "credential": credential.model_dump(mode="json")},
    )
    operation.advance("secret_written")

    restarted = OpenAgentApp(paths)
    assert restarted.credentials.resolve(credential) is None
    assert restarted.journal.pending() == []


def test_startup_completes_interrupted_agent_document_sync(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    app = OpenAgentApp(paths)
    agent = AgentProfile(
        name="journal-agent",
        runtime=AgentRuntime(type=RuntimeType.CLI, cli="codex"),
    )
    app.repos.agents.upsert(agent)
    operation = app.journal.begin("agent_document_sync", {"path": str(app.paths.openagent_md())})
    operation.advance("db_written")
    app.paths.openagent_md().write_text("stale")

    restarted = OpenAgentApp(paths)
    assert "journal-agent" in restarted.paths.openagent_md().read_text()
    assert restarted.journal.pending() == []


def test_journal_never_serializes_provider_secret(tmp_path: Path) -> None:
    app = OpenAgentApp(_paths(tmp_path))
    secret = "prefixless-secret-that-must-not-hit-journal"
    with app.providers.create_transaction(
        name="journalled",
        provider_type="custom",
        base_url="https://example.invalid/v1",
        api_key=secret,
    ) as transaction:
        bodies = [path.read_text() for path in app.paths.journal_dir.glob("*.json")]
        assert bodies and all(secret not in body for body in bodies)
        transaction.commit()


def test_provider_remove_keeps_journal_until_keychain_delete_succeeds(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    app = OpenAgentApp(paths)
    provider = app.providers.add(
        name="journal-remove",
        provider_type="custom",
        base_url="https://example.invalid/v1",
        api_key="prefixless-secret-for-delete-retry",
    )
    backend = keyring.get_keyring()
    original_delete = backend.delete_password

    def fail_delete(_service: str, _username: str) -> None:
        raise RuntimeError("simulated keychain outage")

    backend.delete_password = fail_delete  # type: ignore[method-assign]
    with pytest.raises(CredentialError, match="could not be deleted"):
        app.providers.remove(provider.name)

    assert app.providers.get(provider.name) is None
    assert [operation.kind for operation in app.journal.pending()] == ["provider_remove"]

    backend.delete_password = original_delete  # type: ignore[method-assign]
    restarted = OpenAgentApp(paths)
    assert restarted.journal.pending() == []
    assert restarted.credentials.resolve(provider.credential) is None
