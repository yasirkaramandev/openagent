"""A durably-committed provider must survive recovery — the rc4 data-loss regression.

Background: ``ProviderTransaction.commit()`` durably writes the provider row, then does a *fallible*
legacy-secret cleanup and finally unlinks the journal. It wrote no post-durability marker, so a
committed provider whose journal was not yet unlinked (a crash, a failed legacy ``delete_secret``,
or a failed unlink) left a pending operation at stage ``db_written`` — indistinguishable from a
never-committed add. Startup recovery keyed its delete decision off ``current_revision ==
expected_revision`` alone and deleted the committed provider (and its probes and new secret).

Each test here first failed against the unpatched rc4 implementation. The fix writes a durable
``commit_durable`` stage before the legacy cleanup, marks the rollback path with ``rollback_pending``
before compensating, and makes recovery preserve on any ambiguity — never deleting a row it cannot
prove was rolled back.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import CredentialRef, CredentialType

pytestmark = pytest.mark.security


def _paths(root: Path) -> Paths:
    return Paths(
        data_dir=root / "data",
        config_dir=root / "config",
        db_path=root / "data" / "openagent.db",
        project_root=root / "project",
    )


def _legacy_ref(name: str) -> CredentialRef:
    """The pre-revision, unnamed keychain account a committed add cleans up on success."""

    return CredentialRef(
        type=CredentialType.KEYCHAIN, service="openagent", account=f"provider/{name}"
    )


def _fail_legacy_cleanup(store, name: str):
    """Return a ``delete_secret`` replacement that fails only for the legacy account of ``name``."""

    real_delete = store.delete_secret

    def flaky(credential: CredentialRef, *args: object, **kwargs: object) -> None:
        if credential.account == f"provider/{name}":  # the legacy, pre-revision account
            raise RuntimeError("keychain unavailable during legacy cleanup")
        return real_delete(credential, *args, **kwargs)

    return flaky


def test_committed_provider_survives_legacy_secret_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "project").mkdir()
    app = OpenAgentApp(_paths(tmp_path))
    app.credentials.set_secret(_legacy_ref("acme"), "old-key")
    monkeypatch.setattr(
        app.credentials, "delete_secret", _fail_legacy_cleanup(app.credentials, "acme")
    )

    # The add succeeds and returns the committed provider: a failed legacy-secret cleanup is not part
    # of the atomic commit and must not turn a successful add into an error.
    provider = app.providers.add(
        name="acme", provider_type="custom", base_url="https://api.test/v1", api_key="new-key"
    )
    revision = provider.credential_revision
    assert app.providers.get("acme") is not None
    assert [op.stage for op in app.journal.pending()] == ["legacy_cleanup_pending"]

    monkeypatch.undo()  # the keychain recovers
    restarted = OpenAgentApp(_paths(tmp_path))  # triggers startup recovery
    survivor = restarted.providers.get("acme")
    assert survivor is not None, "recovery deleted a committed provider (data loss)"
    assert survivor.credential_revision == revision
    assert restarted.credentials.resolve(survivor.credential) == "new-key"
    # Recovery finished the deferred legacy cleanup and resolved the operation.
    assert restarted.credentials.resolve(_legacy_ref("acme")) is None
    assert restarted.journal.pending() == []


def test_committed_provider_survives_journal_complete_unlink_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "project").mkdir()
    app = OpenAgentApp(_paths(tmp_path))

    import openagent.security.journal as journal_mod

    def boom_complete(self: object, operation_id: str) -> None:
        raise OSError("cannot unlink journal file")

    monkeypatch.setattr(journal_mod.OperationJournal, "complete", boom_complete)

    with pytest.raises(OSError):
        app.providers.add(
            name="acme",
            provider_type="custom",
            base_url="https://api.test/v1",
            key_env="ACME_KEY",
            credential_source="env",
        )
    # The row is durably committed regardless of the unlink failure at the tail of commit().
    assert app.providers.get("acme") is not None

    monkeypatch.undo()
    restarted = OpenAgentApp(_paths(tmp_path))
    assert restarted.providers.get("acme") is not None, (
        "recovery deleted a committed provider whose journal unlink failed (data loss)"
    )


def test_commit_stage_write_failure_never_triggers_provider_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "project").mkdir()
    app = OpenAgentApp(_paths(tmp_path))

    import openagent.security.journal as journal_mod

    real_write = journal_mod.OperationJournal._write

    def boom_on_commit_durable(self: object, operation: object) -> None:
        if getattr(operation, "stage", None) == "commit_durable":
            raise OSError("cannot persist commit_durable stage")
        return real_write(self, operation)

    monkeypatch.setattr(journal_mod.OperationJournal, "_write", boom_on_commit_durable)

    with pytest.raises(OSError):
        app.providers.add(
            name="acme",
            provider_type="custom",
            base_url="https://api.test/v1",
            key_env="ACME_KEY",
            credential_source="env",
        )
    # Even though writing the durable marker failed, __exit__ must NOT roll a committed row back.
    assert app.providers.get("acme") is not None

    monkeypatch.undo()
    restarted = OpenAgentApp(_paths(tmp_path))
    assert restarted.providers.get("acme") is not None, (
        "an ambiguous operation was resolved by deleting a committed provider (data loss)"
    )


_CRASH_WORKER = textwrap.dedent(
    """
    import os
    import sys
    from pathlib import Path

    from openagent.app import OpenAgentApp
    from openagent.config import Paths

    root = Path(sys.argv[1])
    paths = Paths(
        data_dir=root / "data",
        config_dir=root / "config",
        db_path=root / "data" / "openagent.db",
        project_root=root / "project",
    )
    app = OpenAgentApp(paths)
    tx = app.providers.create_transaction(
        name="acme",
        provider_type="custom",
        base_url="https://acme.invalid/v1",
        credential_source="none",
    )
    tx.__enter__()  # the provider row is now durably committed; the journal op is at db_written
    (root / "committed").write_text(tx.provider.credential_revision, encoding="utf-8")
    os._exit(1)  # hard crash before commit()/__exit__ — no compensation runs
    """
)


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path("src").resolve()) + os.pathsep + env.get("PYTHONPATH", "")
    return env


@pytest.mark.multiprocess
def test_process_crash_after_db_commit_before_legacy_cleanup_preserves_provider(
    tmp_path: Path,
) -> None:
    (tmp_path / "project").mkdir()
    script = tmp_path / "crash.py"
    script.write_text(_CRASH_WORKER, encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_environment(),
    )
    _out, err = proc.communicate(timeout=30)
    assert proc.returncode == 1, err
    committed_revision = (tmp_path / "committed").read_text(encoding="utf-8")

    app = OpenAgentApp(_paths(tmp_path))  # a fresh process replays recovery
    survivor = app.providers.get("acme")
    assert survivor is not None, "recovery deleted a committed provider after a crash (data loss)"
    assert survivor.credential_revision == committed_revision
    with app.db.engine.begin() as conn:
        assert conn.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []


def test_uncommitted_provider_rollback_still_removes_owned_generation(tmp_path: Path) -> None:
    (tmp_path / "project").mkdir()
    app = OpenAgentApp(_paths(tmp_path))
    tx = app.providers.create_transaction(
        name="acme",
        provider_type="custom",
        base_url="https://acme.invalid/v1",
        api_key="acme-secret",
    )
    tx.__enter__()
    assert app.providers.get("acme") is not None
    credential = tx.provider.credential

    # The partner write fails: an uncommitted transaction must still roll its generation back
    # exactly. The fix must not weaken this genuine rollback path.
    tx.__exit__(RuntimeError, RuntimeError("partner write failed"), None)

    assert app.providers.get("acme") is None
    assert app.credentials.resolve(credential) is None
    assert app.journal.pending() == []
    # A restart finds nothing left to compensate.
    restarted = OpenAgentApp(_paths(tmp_path))
    assert restarted.providers.get("acme") is None
    assert restarted.journal.pending() == []


def test_stale_cleanup_never_touches_replacement_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "project").mkdir()
    app = OpenAgentApp(_paths(tmp_path))
    tx_a = app.providers.create_transaction(
        name="acme",
        provider_type="custom",
        base_url="https://gen-a.invalid/v1",
        api_key="secret-a",
    )
    tx_a.__enter__()
    credential_a = tx_a.provider.credential

    # Generation A's rollback cannot finish deleting its row (a simulated DB failure), so its
    # operation stays pending — the "stale cleanup" that must never touch a later generation.
    def fail_delete(_provider_id: str, _revision: str) -> bool:
        raise RuntimeError("simulated database compensation failure")

    monkeypatch.setattr(app.repos.providers, "delete_owned_with_probes", fail_delete)
    tx_a.__exit__(RuntimeError, RuntimeError("partner write failed"), None)
    monkeypatch.undo()

    # Replace the row with generation B.
    assert app.providers.remove("acme")
    generation_b = app.providers.add(
        name="acme",
        provider_type="custom",
        base_url="https://gen-b.invalid/v1",
        api_key="secret-b",
    )

    restarted = OpenAgentApp(_paths(tmp_path))  # replays A's stale compensation
    survivor = restarted.providers.get("acme")
    assert survivor is not None
    assert survivor.credential_revision == generation_b.credential_revision
    assert survivor.base_url == "https://gen-b.invalid/v1"
    assert restarted.credentials.resolve(generation_b.credential) == "secret-b"
    assert restarted.credentials.resolve(credential_a) is None  # only A's own secret was cleaned


def test_provider_agent_create_commit_cleanup_failure_preserves_both_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "project").mkdir()
    app = OpenAgentApp(_paths(tmp_path))
    app.credentials.set_secret(_legacy_ref("acme"), "old-key")
    monkeypatch.setattr(
        app.credentials, "delete_secret", _fail_legacy_cleanup(app.credentials, "acme")
    )

    agent = app.agents.create_with_new_provider(
        provider_name="acme",
        provider_type="custom",
        base_url="https://api.test/v1",
        api_key="new-key",
        credential_source="keychain",
        model="m",
        name="acme-coder",
    )
    assert agent.name == "acme-coder"
    assert app.providers.get("acme") is not None
    bound = app.agents.get("acme-coder")
    assert bound is not None
    assert bound.runtime.provider == "acme"  # the FK binding is intact
    assert [op.stage for op in app.journal.pending()] == ["legacy_cleanup_pending"]

    monkeypatch.undo()
    restarted = OpenAgentApp(_paths(tmp_path))
    survivor = restarted.providers.get("acme")
    survivor_agent = restarted.agents.get("acme-coder")
    assert survivor is not None
    assert survivor_agent is not None
    assert survivor_agent.runtime.provider == "acme"
    assert restarted.credentials.resolve(survivor.credential) == "new-key"
    with restarted.db.engine.begin() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []


def test_provider_recovery_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "project").mkdir()
    app = OpenAgentApp(_paths(tmp_path))
    app.credentials.set_secret(_legacy_ref("acme"), "old-key")
    monkeypatch.setattr(
        app.credentials, "delete_secret", _fail_legacy_cleanup(app.credentials, "acme")
    )
    provider = app.providers.add(
        name="acme", provider_type="custom", base_url="https://api.test/v1", api_key="new-key"
    )
    revision = provider.credential_revision
    monkeypatch.undo()

    # Replaying recovery any number of times must converge on exactly one row, the right secret, an
    # untouched generation and a clean journal — never a second deletion or a corrupted op.
    for _ in range(3):
        restarted = OpenAgentApp(_paths(tmp_path))
        matches = [p for p in restarted.providers.list() if p.name == "acme"]
        assert len(matches) == 1
        assert matches[0].credential_revision == revision
        assert restarted.credentials.resolve(matches[0].credential) == "new-key"

    final = OpenAgentApp(_paths(tmp_path))
    assert final.journal.pending() == []
