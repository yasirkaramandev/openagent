"""Capability evidence is stored with the scope that makes it invalidatable (spec §8).

The v0.1 shape was booleans on a model row. A boolean cannot express "supported, by a live probe,
against credential revision X, over this protocol, at this endpoint, at time T" — and without those,
there is nothing to invalidate *on*. A rotated key would inherit the old key's verdict, and a model
behind a changed base URL would answer a question nobody asked.

Storage is append-only. Two observations of one model under two credentials are two facts; a
destructive UNIQUE across the observation columns would make them collide and the loser would be
lost. Invalidation is therefore a scope query, not a delete, so the history explaining a verdict
survives the verdict changing.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from openagent.core.models import CapabilityEvidenceRecord, ProviderConnection
from openagent.providers.compat.evidence import _RANK, EvidenceSource
from openagent.storage.db import Database
from openagent.storage.repositories import Repositories

SCOPE = {
    "provider_id": "p1",
    "model_id": "m1",
    "credential_revision": "revA",
    "protocol": "openai-chat",
    "base_url_fingerprint": "fp1",
}


@pytest.fixture()
def repos() -> Repositories:
    db = Database.in_memory()
    bundle = Repositories(db)
    bundle.providers.create(
        ProviderConnection(id="p1", name="P", provider_type="openai", credential_revision="revA")
    )
    return bundle


def _record(**overrides: object) -> CapabilityEvidenceRecord:
    base: dict[str, object] = {
        "provider_id": "p1",
        "model_id": "m1",
        "capability": "tools",
        "status": "supported",
        "source": "live_probe",
        "credential_revision": "revA",
        "protocol": "openai-chat",
        "base_url_fingerprint": "fp1",
        "observed_at": datetime.now(timezone.utc),
    }
    base.update(overrides)
    return CapabilityEvidenceRecord(**base)  # type: ignore[arg-type]


def test_legacy_migration_is_a_parseable_source(repos: Repositories) -> None:
    """Migration 0016 writes ``legacy_migration``; the enum had no such member.

    Every row the migration produced therefore raised ValueError on read — the migration wrote
    evidence nothing could load.
    """

    assert EvidenceSource("legacy_migration") is EvidenceSource.LEGACY_MIGRATION
    repos.capability_evidence.append(_record(source="legacy_migration", observed_at=None))
    stored = repos.capability_evidence.list_for_model("p1", "m1")
    assert stored[0].source == "legacy_migration"


def test_legacy_migration_is_the_weakest_automatic_source() -> None:
    """A v0.1 guess must not outrank a real catalog reading."""

    assert _RANK[EvidenceSource.LEGACY_MIGRATION] == 5
    assert _RANK[EvidenceSource.LEGACY_MIGRATION] < _RANK[EvidenceSource.CURATED_PRESET]
    assert _RANK[EvidenceSource.LEGACY_MIGRATION] < _RANK[EvidenceSource.LIVE_PROBE]


def test_an_unknown_observation_time_is_null_not_empty_string(repos: Repositories) -> None:
    """A legacy row does not know when it was observed. "" in a datetime is a deferred crash."""

    repos.capability_evidence.append(_record(source="legacy_migration", observed_at=None))
    assert repos.capability_evidence.list_for_model("p1", "m1")[0].observed_at is None


def test_evidence_is_valid_only_for_the_endpoint_that_produced_it(repos: Repositories) -> None:
    repos.capability_evidence.append(_record())
    assert len(repos.capability_evidence.list_valid(**SCOPE)) == 1
    for changed in (
        {"credential_revision": "revB"},
        {"base_url_fingerprint": "fp2"},
        {"protocol": "openai-responses"},
        {"region": "eu"},
        {"workspace_id": "ws2"},
    ):
        assert repos.capability_evidence.list_valid(**{**SCOPE, **changed}) == [], (
            f"evidence survived a change of {list(changed)[0]}"
        )


def test_rotation_invalidates_without_destroying_history(repos: Repositories) -> None:
    """The rows stay as audit history; they simply stop matching the scope filter."""

    repos.capability_evidence.append(_record())
    repos.capability_evidence.append(_record(capability="streaming"))
    assert repos.capability_evidence.list_valid(**{**SCOPE, "credential_revision": "revB"}) == []
    assert len(repos.capability_evidence.list_for_model("p1", "m1")) == 2
    assert repos.capability_evidence.count_superseded_by_credential("p1", "revB") == 2


def test_two_credentials_produce_two_facts_not_a_collision(repos: Repositories) -> None:
    """The reason there is no broad destructive UNIQUE across the observation columns."""

    repos.capability_evidence.append(_record(credential_revision="revA"))
    repos.capability_evidence.append(_record(credential_revision="revB"))
    assert len(repos.capability_evidence.list_for_model("p1", "m1")) == 2
    assert len(repos.capability_evidence.list_valid(**SCOPE)) == 1


def test_deleting_a_provider_removes_its_evidence(repos: Repositories) -> None:
    repos.capability_evidence.append(_record())
    assert repos.capability_evidence.delete_for_provider("p1") == 1
    assert repos.capability_evidence.list_for_provider("p1") == []


def test_decode_report_separates_good_rows_from_bad(repos: Repositories) -> None:
    repos.capability_evidence.append(_record())
    with repos.db.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO capability_evidence "
            "(provider_id, model_id, capability, status, source, observed_at) "
            "VALUES ('p1','m1','tools','supported','live_probe','not-a-timestamp')"
        )
    good, bad = repos.capability_evidence.decode_report()
    assert len(good) == 1
    assert len(bad) == 1
    # The descriptor names the record and the problem, never the payload.
    assert bad[0]["table"] == "capability_evidence"
    assert "not-a-timestamp" not in str(bad[0])


def test_the_fresh_schema_matches_what_the_migration_builds(repos: Repositories) -> None:
    """A fresh database and an upgraded one must not have different schemas."""

    with repos.db.engine.connect() as conn:
        columns = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(capability_evidence)")}
    assert {
        "provider_id",
        "model_id",
        "capability",
        "status",
        "source",
        "observed_at",
        "probe_version",
        "provider_version",
        "model_revision",
        "credential_revision",
        "protocol",
        "base_url_fingerprint",
        "region",
        "workspace_id",
        "detail",
    } <= columns
