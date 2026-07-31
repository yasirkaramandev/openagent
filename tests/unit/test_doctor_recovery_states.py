"""Doctor surfaces provider-recovery states — redacted — so a preserved ambiguity is never silent.

When recovery preserves a provider it cannot prove was rolled back (rather than risk deleting
committed data), the only signal a user gets is Doctor. It must name the distinct recovery states
(ambiguous ownership, deferred legacy-secret cleanup, rollback pending, superseded generation)
without ever rendering the journal payload, which can carry a credential ref, header or URL.
"""

from __future__ import annotations

from pathlib import Path

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.services.doctor_service import WARN, DoctorService


def _app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "project"
    project.mkdir()
    return OpenAgentApp(
        Paths(
            data_dir=tmp_path / "data",
            config_dir=tmp_path / "config",
            db_path=tmp_path / "data" / "openagent.db",
            project_root=project,
        )
    )


def test_recovery_state_labels() -> None:
    state = DoctorService._recovery_state
    assert state("provider_add", "recovery_ambiguous") == "provider recovery ownership ambiguous"
    assert state("provider_add", "rollback_pending") == "provider rollback pending"
    assert state("provider_add", "owned_compensation") == "provider rollback pending"
    assert state("provider_add", "commit_durable") == "provider legacy credential cleanup pending"
    assert (
        state("provider_add", "legacy_cleanup_pending")
        == "provider legacy credential cleanup pending"
    )
    assert state("provider_add", "superseded_generation") == "provider generation superseded"
    assert state("provider_remove", "superseded_generation") == "provider generation superseded"
    # An unknown/legacy provider stage is a benign "keep retrying", never a hard error.
    assert state("provider_add", "db_written") == "provider recovery retry pending"
    assert state("agent_document_sync", "db_written") == "OPENAGENT.md sync pending"


def test_journal_check_surfaces_recovery_states_without_leaking_payload(tmp_path: Path) -> None:
    app = _app(tmp_path)  # startup recovery has already run; the ops below stay pending

    ambiguous = app.journal.begin(
        "provider_add",
        {
            "provider_id": "provider_acme",
            "credential_revision": "rev-do-not-render",
            "credential": {"type": "keychain", "account": "provider/acme/rev-do-not-render"},
        },
    )
    ambiguous.advance("recovery_ambiguous")
    legacy = app.journal.begin(
        "provider_add",
        {"provider_id": "provider_beta", "credential_revision": "rev-beta"},
    )
    legacy.advance("legacy_cleanup_pending")

    check = app.doctor._journal_check()

    assert check.status == WARN
    assert "provider recovery ownership ambiguous" in check.detail
    assert "provider legacy credential cleanup pending" in check.detail
    assert check.data["states"]["provider recovery ownership ambiguous"] == 1
    assert check.data["states"]["provider legacy credential cleanup pending"] == 1
    # The payload (revision token, account) is never rendered in the detail or the structured data.
    rendered = check.detail + repr(check.data)
    assert "rev-do-not-render" not in rendered
    assert "provider/acme" not in rendered
