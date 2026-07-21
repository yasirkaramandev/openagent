"""Atomic provider+agent creation and rollback (item 3).

`AgentService.create_with_new_provider` must leave the system exactly as it started whenever agent
creation fails after the provider was written: no provider row, no keychain secret, no half-written
OPENAGENT.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import CredentialRef, CredentialType, RuntimeType
from openagent.credentials.store import CredentialError
from openagent.services.agent_service import AgentError


def _app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    paths = Paths(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db",
        project_root=project,
    )
    return OpenAgentApp(paths)


def _md_missing_agent(oa: OpenAgentApp, agent: str) -> bool:
    md = oa.paths.openagent_md()
    return (not md.exists()) or (f"`{agent}`" not in md.read_text(encoding="utf-8"))


def test_happy_path_creates_both(tmp_path: Path) -> None:
    oa = _app(tmp_path)
    agent = oa.agents.create_with_new_provider(
        provider_name="ds",
        provider_type="custom",
        base_url="https://api.test/v1",
        key_env="DS_KEY",
        credential_source="env",
        model="m",
        name="ds-coder",
    )
    assert agent.name == "ds-coder"
    assert oa.providers.get("ds") is not None
    assert not _md_missing_agent(oa, "ds-coder")


def test_duplicate_agent_name_leaves_no_provider(tmp_path: Path) -> None:
    oa = _app(tmp_path)
    oa.agents.create(name="dup", runtime_type=RuntimeType.CLI, cli="codex")
    with pytest.raises(AgentError):
        oa.agents.create_with_new_provider(
            provider_name="ds",
            provider_type="custom",
            base_url="https://api.test/v1",
            key_env="DS_KEY",
            credential_source="env",
            model="m",
            name="dup",
        )
    assert oa.providers.get("ds") is None


def test_provider_create_is_db_authoritative_not_the_service_precheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The uniqueness decision is the database's, not the service's check-then-act (spec §7).

    Simulate the race window by forcing the service pre-check to see no provider (as a second
    process would, having read before the first committed). The insert-only ``create`` must still
    reject the duplicate at the database, surface it as a validation error, and leave the existing
    provider row and its secret untouched.
    """

    from openagent.services.provider_service import ProviderValidationError

    oa = _app(tmp_path)
    oa.providers.add(
        name="acme", provider_type="custom", base_url="https://api.test/v1", api_key="first-key"
    )
    winner = oa.providers.get("acme")
    assert winner is not None

    # The pre-check now lies (returns None), exactly as it would in a lost check-then-act race.
    monkeypatch.setattr(oa.providers, "get", lambda _name: None)
    with pytest.raises(ProviderValidationError):
        oa.providers.add(
            name="acme",
            provider_type="custom",
            base_url="https://api.test/v1",
            api_key="second-key",
        )

    monkeypatch.undo()
    # The first writer is untouched: one row, and its original secret survives.
    still = oa.providers.get("acme")
    assert still is not None
    assert still.credential_revision == winner.credential_revision
    assert oa.credentials.resolve(winner.credential) == "first-key"


def test_agent_create_is_db_authoritative_not_the_service_precheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same insert-only guarantee for agents (spec §8): the DB rejects the duplicate name."""

    oa = _app(tmp_path)
    oa.agents.create(name="bot", runtime_type=RuntimeType.CLI, cli="codex")

    monkeypatch.setattr(oa.repos.agents, "get", lambda _name: None)
    with pytest.raises(AgentError):
        oa.agents.create(name="bot", runtime_type=RuntimeType.CLI, cli="codex")

    monkeypatch.undo()
    assert len([a for a in oa.agents.list() if a.name == "bot"]) == 1


def test_agent_validation_failure_leaves_no_provider(tmp_path: Path) -> None:
    oa = _app(tmp_path)
    with pytest.raises(AgentError):
        oa.agents.create_with_new_provider(
            provider_name="ds",
            provider_type="custom",
            base_url="https://api.test/v1",
            key_env="DS_KEY",
            credential_source="env",
            model="",
            name="ds-coder",
        )
    assert oa.providers.get("ds") is None


def test_openagent_md_write_failure_rolls_back_provider_and_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oa = _app(tmp_path)

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("openagent.services.agent_service.write_openagent_md", boom)
    with pytest.raises(OSError):
        oa.agents.create_with_new_provider(
            provider_name="ds",
            provider_type="custom",
            base_url="https://api.test/v1",
            key_env="DS_KEY",
            credential_source="env",
            model="m",
            name="ds-coder",
        )
    # Provider row rolled back, agent row rolled back.
    assert oa.providers.get("ds") is None
    assert oa.agents.get("ds-coder") is None


def test_keychain_write_failure_persists_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oa = _app(tmp_path)

    def boom(*_a: object, **_k: object) -> None:
        raise CredentialError("keyring unavailable")

    monkeypatch.setattr(oa.credentials, "set_secret", boom)
    with pytest.raises(CredentialError):
        oa.agents.create_with_new_provider(
            provider_name="ds",
            provider_type="custom",
            base_url="https://api.test/v1",
            api_key="sk-secret",
            credential_source="keychain",
            model="m",
            name="ds-coder",
        )
    assert oa.providers.get("ds") is None
    assert oa.agents.get("ds-coder") is None


def test_provider_repository_failure_persists_no_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oa = _app(tmp_path)

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("db is locked")

    monkeypatch.setattr(oa.repos.providers, "create", boom)
    with pytest.raises(RuntimeError):
        oa.agents.create_with_new_provider(
            provider_name="ds",
            provider_type="custom",
            base_url="https://api.test/v1",
            key_env="DS_KEY",
            credential_source="env",
            model="m",
            name="ds-coder",
        )
    assert oa.providers.get("ds") is None
    assert oa.agents.get("ds-coder") is None


# --------------------------------------------------------------------------- keychain rollback (item 17)


def _ref(name: str) -> CredentialRef:
    return CredentialRef(
        type=CredentialType.KEYCHAIN, service="openagent", account=f"provider/{name}"
    )


def test_failed_provider_write_restores_the_previous_secret_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed provider write must put the user's *old* key back, byte for byte (item 17).

    The old rollback only tracked whether it had written a secret where none existed. When one *did*
    exist it was overwritten and then left there — so a failure destroyed the user's working key and
    replaced it with a key belonging to a provider that was never saved. Checking `is not None` is
    not enough; the previous **value** has to be restored.
    """

    oa = _app(tmp_path)
    ref = _ref("acme")
    oa.credentials.set_secret(ref, "old-secret")

    def boom(_provider):
        raise RuntimeError("db is down")

    monkeypatch.setattr(oa.repos.providers, "create", boom)

    with pytest.raises(RuntimeError, match="db is down"):
        oa.providers.add(
            name="acme",
            provider_type="custom",
            base_url="https://api.test/v1",
            api_key="new-secret",
        )

    assert oa.credentials.resolve(ref) == "old-secret"
    assert oa.providers.get("acme") is None


def test_failed_provider_write_removes_a_secret_that_did_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oa = _app(tmp_path)
    ref = _ref("acme")
    assert oa.credentials.resolve(ref) is None

    monkeypatch.setattr(
        oa.repos.providers, "create", lambda _p: (_ for _ in ()).throw(RuntimeError("db is down"))
    )

    with pytest.raises(RuntimeError):
        oa.providers.add(
            name="acme",
            provider_type="custom",
            base_url="https://api.test/v1",
            api_key="new-secret",
        )

    assert oa.credentials.resolve(ref) is None  # nothing orphaned in the keychain


def test_agent_transaction_rollback_restores_the_previous_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider+agent transaction follows the same rule: a pre-existing secret survives."""

    oa = _app(tmp_path)
    ref = _ref("acme")
    oa.credentials.set_secret(ref, "old-secret")

    monkeypatch.setattr(
        oa.repos.agents,
        "create",
        lambda _a: (_ for _ in ()).throw(RuntimeError("agent write failed")),
    )

    with pytest.raises(RuntimeError):
        oa.agents.create_with_new_provider(
            provider_name="acme",
            provider_type="custom",
            base_url="https://api.test/v1",
            api_key="new-secret",
            model="m",
            name="acme-coder",
        )

    assert oa.credentials.resolve(ref) == "old-secret"  # the user's old key is intact
    assert oa.providers.get("acme") is None  # …and the half-made provider is gone


# --------------------------------------------------------------------------- transaction-local rollback (§6)


def test_successful_add_retains_no_rollback_secret_cache(tmp_path: Path) -> None:
    """A successful ``provider add`` must not leave the previous key in a service-level cache (§6).

    The old design kept a ``SecretRollback`` (previous key in plaintext) in ``ProviderService`` until
    a rollback that, on success, never came — so the value lived for the whole process. There is now
    no such cache at all: rollback state lives only on the transaction stack.
    """

    oa = _app(tmp_path)
    oa.providers.add(
        name="acme", provider_type="custom", base_url="https://api.test/v1", api_key="new-secret"
    )
    assert not hasattr(oa.providers, "_rollbacks")
    assert oa.providers.get("acme") is not None


def test_successful_provider_agent_transaction_clears_previous_secret(tmp_path: Path) -> None:
    oa = _app(tmp_path)
    ref = _ref("acme")
    oa.credentials.set_secret(ref, "old-secret")  # a prior key exists under this account

    oa.agents.create_with_new_provider(
        provider_name="acme",
        provider_type="custom",
        base_url="https://api.test/v1",
        api_key="new-secret",
        model="m",
        name="acme-coder",
    )
    # Success: the new key lives under the connection's revision-scoped account; the legacy account
    # is removed only after the provider row is durable.
    provider = oa.providers.get("acme")
    assert provider is not None
    assert oa.credentials.resolve(provider.credential) == "new-secret"
    assert oa.credentials.resolve(ref) is None
    assert not hasattr(oa.providers, "_rollbacks")


def test_new_key_with_no_previous_value_is_deleted_on_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no key existed before, a rolled-back transaction removes the one it wrote (no orphan)."""

    oa = _app(tmp_path)
    ref = _ref("acme")
    assert oa.credentials.resolve(ref) is None
    monkeypatch.setattr(
        oa.repos.agents,
        "create",
        lambda _a: (_ for _ in ()).throw(RuntimeError("agent write failed")),
    )
    with pytest.raises(RuntimeError):
        oa.agents.create_with_new_provider(
            provider_name="acme",
            provider_type="custom",
            base_url="https://api.test/v1",
            api_key="brand-new",
            model="m",
            name="acme-coder",
        )
    assert oa.credentials.resolve(ref) is None
    assert oa.providers.get("acme") is None


def test_stale_rollback_cannot_affect_a_later_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rolled-back later transaction must not touch an earlier, unrelated provider's key (§6)."""

    oa = _app(tmp_path)
    # 1) A successful earlier add with its own key.
    oa.providers.add(
        name="alpha", provider_type="custom", base_url="https://api.test/v1", api_key="alpha-key"
    )
    # 2) A later, unrelated provider whose agent creation fails and rolls the provider back.
    monkeypatch.setattr(
        oa.repos.agents,
        "create",
        lambda _a: (_ for _ in ()).throw(RuntimeError("agent write failed")),
    )
    with pytest.raises(RuntimeError):
        oa.agents.create_with_new_provider(
            provider_name="beta",
            provider_type="custom",
            base_url="https://api.test/v1",
            api_key="beta-key",
            model="m",
            name="beta-coder",
        )
    # alpha's key is untouched by beta's rollback; alpha still exists, beta does not.
    alpha = oa.providers.get("alpha")
    assert alpha is not None
    assert oa.credentials.resolve(alpha.credential) == "alpha-key"
    assert oa.providers.get("alpha") is not None
    assert oa.providers.get("beta") is None
