"""Model discovery + capability probing + cache (spec §25).

Model IDs must never be hardcoded — providers rotate them constantly (spec §15, §25). This module
lists a provider's models and probes a specific model's capabilities, caching results with a TTL.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..core.models import ModelCapabilities, ModelProfile, RemoteModel
from .base import ProviderAdapter

DEFAULT_TTL = timedelta(days=7)


async def discover_models(adapter: ProviderAdapter) -> list[RemoteModel]:
    """List models the provider exposes (empty list if it has no ``/models``)."""

    try:
        return await adapter.list_models()
    except Exception:  # noqa: BLE001 - discovery is best-effort
        return []


async def probe_capabilities(adapter: ProviderAdapter, model_id: str) -> ModelCapabilities:
    """Run the adapter's capability probe (spec §25.2)."""

    return await adapter.probe_model(model_id)


def capabilities_fresh(profile: ModelProfile, ttl: timedelta = DEFAULT_TTL) -> bool:
    """Whether a model's cached capabilities are still within the TTL (spec §25.3)."""

    if profile.capabilities_tested_at is None:
        return False
    tested = profile.capabilities_tested_at
    if tested.tzinfo is None:
        tested = tested.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - tested < ttl


def apply_probe(profile: ModelProfile, probed: ModelCapabilities) -> ModelProfile:
    """Merge probe results into a model profile and stamp the test time."""

    merged = profile.capabilities.merge(probed)
    return profile.model_copy(
        update={"capabilities": merged, "capabilities_tested_at": datetime.now(timezone.utc)}
    )
