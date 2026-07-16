"""A capability probe must outlive the process that ran it (spec §22).

The probe is the gate on ``agent add`` for a mixed catalog (NVIDIA Build): a model id proves
nothing there, so an unprobed model is refused unless the user passes ``--allow-unverified-model``.
A process-local cache makes that gate unusable from the CLI, where *every command is a new
process* — `provider probe` would verify a model and the very next `add` would demand the override
anyway, training the user to pass the flag that disables the check.

These tests drive two ``OpenAgentApp`` instances over one DB file. That is a faithful stand-in for
two processes: each builds its own ``ProviderService``, so nothing survives in Python memory and
only what reached SQLite can be read back.

The invalidation cases matter as much as the hit: a stored "verified" that outlives the thing it
was verified against is a lie with a longer lifetime than the truth it replaced.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import ModelCapabilities, Protocol
from openagent.providers.discovery import (
    PROBE_VERIFIED,
    AgentModelProbe,
)
from openagent.services import provider_service as provider_service_module

MODEL = "meta/llama-3.1-8b-instruct"
KEY = "nvapi-test-secret-value-do-not-persist-0123456789"


def _paths(tmp_path: Path) -> Paths:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    return Paths(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db",
        project_root=project,
    )


def _app(tmp_path: Path) -> OpenAgentApp:
    """A brand-new app on the same DB file — i.e. what the *next* CLI process sees."""
    return OpenAgentApp(_paths(tmp_path))


def _add_provider(app: OpenAgentApp, *, name: str = "nvidia-build") -> None:
    app.providers.add(
        name=name,
        provider_type="nvidia-build",
        api_key=KEY,
    )


@pytest.fixture()
def verified_probe(monkeypatch: pytest.MonkeyPatch) -> AgentModelProbe:
    """Stub the network probe: these tests are about persistence, not about HTTP."""

    probe = AgentModelProbe(
        model=MODEL,
        capabilities=ModelCapabilities(text=True, streaming=True, tool_calling=True),
        agent_compatible=True,
        category=PROBE_VERIFIED,
        detail="ok",
        tested_at=datetime.now(timezone.utc),
    )

    async def fake_probe(adapter: object, model_id: str) -> AgentModelProbe:
        return replace(probe, model=model_id)

    monkeypatch.setattr(provider_service_module, "probe_agent_model", fake_probe)
    return probe


async def test_probe_is_readable_from_a_fresh_process(
    tmp_path: Path, verified_probe: AgentModelProbe
) -> None:
    """`provider probe` then, in a new process, `add` — the acceptance flow in §22."""

    first = _app(tmp_path)
    _add_provider(first)
    probed = await first.providers.probe_model("nvidia-build", MODEL)
    assert probed.category == PROBE_VERIFIED

    # The process exits here. Nothing of `first` may be consulted below.
    second = _app(tmp_path)
    cached = second.providers.cached_probe("nvidia-build", MODEL)

    assert cached is not None, "the probe did not survive the process that ran it"
    assert cached.category == PROBE_VERIFIED
    assert cached.agent_compatible is True
    assert cached.capabilities.tool_calling is True


async def test_probe_does_not_persist_key_material(
    tmp_path: Path, verified_probe: AgentModelProbe
) -> None:
    """§22 forbids storing the key, a hash of it, or the Authorization header."""

    import hashlib

    app = _app(tmp_path)
    _add_provider(app)
    await app.providers.probe_model("nvidia-build", MODEL)

    raw = (tmp_path / "data" / "openagent.db").read_bytes()
    assert KEY.encode() not in raw
    assert hashlib.sha256(KEY.encode()).hexdigest().encode() not in raw
    assert hashlib.sha256(KEY.encode()).hexdigest()[:16].encode() not in raw
    assert b"Authorization" not in raw
    assert b"Bearer" not in raw


async def _probe_then_new_process(tmp_path: Path) -> OpenAgentApp:
    """Probe in one 'process', assert it survived, and hand back the next 'process'.

    The positive control matters: every invalidation test below asserts a ``None``, and a lookup
    that is broken outright returns ``None`` too. Without proving the probe was readable *first*,
    each of these would pass just as happily against a store that never persisted anything.
    """

    first = _app(tmp_path)
    _add_provider(first)
    await first.providers.probe_model("nvidia-build", MODEL)

    second = _app(tmp_path)
    assert second.providers.cached_probe("nvidia-build", MODEL) is not None, (
        "precondition failed: the probe was not persisted, so this test would pass vacuously"
    )
    return second


async def test_probe_is_invalid_after_the_base_url_changes(
    tmp_path: Path, verified_probe: AgentModelProbe
) -> None:
    app = await _probe_then_new_process(tmp_path)

    provider = app.providers.get("nvidia-build")
    assert provider is not None
    app.repos.providers.upsert(
        provider.model_copy(update={"base_url": "https://elsewhere.example/v1"})
    )

    assert _app(tmp_path).providers.cached_probe("nvidia-build", MODEL) is None


async def test_probe_is_invalid_after_the_protocol_changes(
    tmp_path: Path, verified_probe: AgentModelProbe
) -> None:
    """A connection that now speaks a different wire shape was never probed in that shape."""

    app = await _probe_then_new_process(tmp_path)

    provider = app.providers.get("nvidia-build")
    assert provider is not None
    app.repos.providers.upsert(
        provider.model_copy(update={"protocol": Protocol.ANTHROPIC_MESSAGES})
    )

    assert _app(tmp_path).providers.cached_probe("nvidia-build", MODEL) is None


async def test_probe_is_invalid_after_the_credential_changes(
    tmp_path: Path, verified_probe: AgentModelProbe
) -> None:
    """A key rotated *through OpenAgent* must not inherit the old key's verdict.

    Rotation is remove + re-add, which reuses the id (it is derived from the name) — so this only
    passes because the connection carries a fresh credential revision.
    """

    app = await _probe_then_new_process(tmp_path)
    app.providers.remove("nvidia-build")

    rotated = _app(tmp_path)
    rotated.providers.add(
        name="nvidia-build",
        provider_type="nvidia-build",
        api_key="nvapi-a-completely-different-key-987654",
    )
    assert (
        rotated.providers.get("nvidia-build").id == "provider_nvidia-build"
    )  # id really is reused

    assert _app(tmp_path).providers.cached_probe("nvidia-build", MODEL) is None


async def test_probe_is_invalid_after_the_probe_version_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verified_probe: AgentModelProbe
) -> None:
    await _probe_then_new_process(tmp_path)

    monkeypatch.setattr(provider_service_module, "PROBE_VERSION", "99")
    assert _app(tmp_path).providers.cached_probe("nvidia-build", MODEL) is None


async def test_expired_probe_is_not_reported_as_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verified_probe: AgentModelProbe
) -> None:
    await _probe_then_new_process(tmp_path)

    monkeypatch.setattr(provider_service_module, "PROBE_CACHE_TTL", timedelta(seconds=-1))
    assert _app(tmp_path).providers.cached_probe("nvidia-build", MODEL) is None


async def test_probe_for_one_model_does_not_vouch_for_another(
    tmp_path: Path, verified_probe: AgentModelProbe
) -> None:
    app = await _probe_then_new_process(tmp_path)
    assert app.providers.cached_probe("nvidia-build", "nvidia/other-model") is None


async def test_removing_a_provider_purges_its_probes(
    tmp_path: Path, verified_probe: AgentModelProbe
) -> None:
    """Verdicts must not outlive the connection they describe."""

    app = await _probe_then_new_process(tmp_path)
    assert _probe_rows(app, "provider_nvidia-build") == 1

    app.providers.remove("nvidia-build")

    assert _probe_rows(app, "provider_nvidia-build") == 0


def _probe_rows(app: OpenAgentApp, provider_id: str) -> int:
    from sqlalchemy import func, select

    from openagent.storage import db as t

    with app.db.engine.connect() as conn:
        return int(
            conn.execute(
                select(func.count())
                .select_from(t.model_probes)
                .where(t.model_probes.c.provider_id == provider_id)
            ).scalar_one()
        )
