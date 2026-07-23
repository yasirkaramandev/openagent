"""Provider compensation must be scoped to the immutable credential generation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths

pytestmark = [pytest.mark.security, pytest.mark.multiprocess]


_STALE_ADD_WORKER = textwrap.dedent(
    """
    import json
    import sys
    import time
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
        base_url="https://generation-a.invalid/v1",
        credential_source="none",
    )
    tx.__enter__()
    (root / "generation_a_written").write_text(tx.provider.credential_revision, encoding="utf-8")
    deadline = time.monotonic() + 20
    while not (root / "generation_b_written").exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("timed out waiting for replacement generation")
        time.sleep(0.01)
    tx.__exit__(RuntimeError, RuntimeError("partner write failed"), None)
    print(json.dumps({"rolled_back_revision": tx.provider.credential_revision}))
    """
)


_REPLACE_WORKER = textwrap.dedent(
    """
    import json
    import sys
    import time
    from pathlib import Path

    from openagent.app import OpenAgentApp
    from openagent.config import Paths

    root = Path(sys.argv[1])
    deadline = time.monotonic() + 20
    while not (root / "generation_a_written").exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("timed out waiting for generation A")
        time.sleep(0.01)
    paths = Paths(
        data_dir=root / "data",
        config_dir=root / "config",
        db_path=root / "data" / "openagent.db",
        project_root=root / "project",
    )
    app = OpenAgentApp(paths)
    assert app.providers.remove("acme")
    replacement = app.providers.add(
        name="acme",
        provider_type="custom",
        base_url="https://generation-b.invalid/v1",
        credential_source="none",
    )
    (root / "generation_b_written").write_text(
        replacement.credential_revision, encoding="utf-8"
    )
    print(json.dumps({"replacement_revision": replacement.credential_revision}))
    """
)


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    source = str(Path("src").resolve())
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _paths(root: Path) -> Paths:
    return Paths(
        data_dir=root / "data",
        config_dir=root / "config",
        db_path=root / "data" / "openagent.db",
        project_root=root / "project",
    )


def test_stale_add_rollback_cannot_delete_replacement_generation(tmp_path: Path) -> None:
    (tmp_path / "project").mkdir()
    stale_script = tmp_path / "stale.py"
    replacement_script = tmp_path / "replacement.py"
    stale_script.write_text(_STALE_ADD_WORKER, encoding="utf-8")
    replacement_script.write_text(_REPLACE_WORKER, encoding="utf-8")

    stale = subprocess.Popen(
        [sys.executable, str(stale_script), str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_environment(),
    )
    replacement = subprocess.Popen(
        [sys.executable, str(replacement_script), str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_environment(),
    )
    replacement_out, replacement_err = replacement.communicate(timeout=30)
    stale_out, stale_err = stale.communicate(timeout=30)
    assert replacement.returncode == 0, replacement_err
    assert stale.returncode == 0, stale_err

    expected_revision = json.loads(replacement_out)["replacement_revision"]
    assert json.loads(stale_out)["rolled_back_revision"] != expected_revision
    app = OpenAgentApp(_paths(tmp_path))
    survivor = app.providers.get("acme")
    assert survivor is not None, "stale compensation deleted the replacement provider generation"
    assert survivor.credential_revision == expected_revision
    assert survivor.base_url == "https://generation-b.invalid/v1"


def test_transaction_rollback_deletes_only_its_revision_scoped_secret(tmp_path: Path) -> None:
    (tmp_path / "project").mkdir()
    app = OpenAgentApp(_paths(tmp_path))
    tx = app.providers.create_transaction(
        name="acme",
        provider_type="custom",
        base_url="https://generation-a.invalid/v1",
        api_key="generation-a-secret",
    )
    tx.__enter__()
    generation_a_credential = tx.provider.credential
    assert app.providers.remove("acme")
    replacement = app.providers.add(
        name="acme",
        provider_type="custom",
        base_url="https://generation-b.invalid/v1",
        api_key="generation-b-secret",
    )

    tx.__exit__(RuntimeError, RuntimeError("partner write failed"), None)

    survivor = app.providers.get("acme")
    assert survivor is not None
    assert survivor.credential_revision == replacement.credential_revision
    assert app.credentials.resolve(replacement.credential) == "generation-b-secret"
    assert app.credentials.resolve(generation_a_credential) is None


def test_rollback_keeps_owned_journal_when_database_compensation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "project").mkdir()
    app = OpenAgentApp(_paths(tmp_path))
    tx = app.providers.create_transaction(
        name="acme",
        provider_type="custom",
        base_url="https://generation-a.invalid/v1",
        credential_source="none",
    )
    tx.__enter__()

    def fail_delete(_provider_id: str, _revision: str) -> bool:
        raise RuntimeError("simulated database compensation failure")

    monkeypatch.setattr(app.repos.providers, "delete_owned_with_probes", fail_delete)
    tx.__exit__(RuntimeError, RuntimeError("partner write failed"), None)

    assert app.providers.get("acme") is not None
    # The rollback path durably marks itself before compensating, so a retry after a failed DB
    # compensation is provably a rollback (not an ambiguous ``db_written``) and recovery may finish
    # deleting the owned generation.
    assert [op.stage for op in app.journal.pending()] == ["rollback_pending"]


def test_rollback_still_deletes_owned_row_when_secret_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "project").mkdir()
    app = OpenAgentApp(_paths(tmp_path))
    tx = app.providers.create_transaction(
        name="acme",
        provider_type="custom",
        base_url="https://generation-a.invalid/v1",
        api_key="generation-a-secret",
    )
    tx.__enter__()

    def fail_secret_cleanup(*_args, **_kwargs) -> None:
        raise RuntimeError("simulated secret cleanup failure")

    monkeypatch.setattr(app.credentials, "delete_secret", fail_secret_cleanup)
    with pytest.raises(RuntimeError, match="secret cleanup failure"):
        tx.__exit__(RuntimeError, RuntimeError("partner write failed"), None)

    assert app.providers.get("acme") is None
    assert [op.stage for op in app.journal.pending()] == ["owned_compensation"]
