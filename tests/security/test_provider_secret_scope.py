"""A provider key must be scrubbed at the boundary, and must not outlive the call (spec §8).

``test_config`` / ``remote_models_config`` / ``probe_model_config`` build a transient adapter from a
key the user has typed into a form. They returned the provider's raw exception text as ``detail``
(``str(exc)``) and relied on two things to stay safe:

* the key having been added to a **global registry that was never emptied**, and
* the *UI* redacting on the way to the screen.

That is backwards. It made the leak the UI's problem, kept every key the process had ever seen
resident for its whole lifetime, and left ``detail`` genuinely carrying the key for any caller that
did not happen to route it through the TUI — ``--json`` output, a log line, an exception message.

Redaction belongs where the secret is known to be live: inside the scope, at the point the value
leaves the service.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.credentials.redaction import active_secret_count, clear_registered_secrets

# Prefixless: the patterns cannot catch this one, so only the registry can — which is exactly why
# releasing it before redacting is a leak rather than a cosmetic issue.
PREFIXLESS = "haidian0099887766prefixlesskey"


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


@pytest.fixture()
def oa_app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    return OpenAgentApp(
        Paths(
            data_dir=tmp_path / "data",
            config_dir=tmp_path / "config",
            db_path=tmp_path / "data" / "openagent.db",
            project_root=project,
        )
    )


@pytest.fixture()
def echoing_provider(monkeypatch: pytest.MonkeyPatch):
    """A provider that echoes the API key back in its error, which real providers do."""

    from openagent.services import provider_service

    class _Echo:
        async def test_connection(self):
            raise RuntimeError(f"401 Unauthorized: invalid api key {PREFIXLESS}")

        async def list_models(self):
            raise RuntimeError(f"401 Unauthorized: invalid api key {PREFIXLESS}")

        async def aclose(self):
            return None

    monkeypatch.setattr(provider_service, "build_adapter", lambda *a, **k: _Echo())
    return _Echo


async def _test_config(app):
    return await app.providers.test_config(
        provider_type="openai", base_url="https://example.invalid/v1", api_key=PREFIXLESS
    )


async def test_the_key_never_appears_in_a_returned_error_detail(oa_app, echoing_provider):
    """The headline: the service must not hand its caller a string containing the key."""

    result = await _test_config(oa_app)
    assert not result.ok
    assert PREFIXLESS not in result.detail, (
        "test_config returned the provider's raw error, which contained the API key"
    )


async def test_the_key_does_not_outlive_the_call(oa_app, echoing_provider):
    """A form-scoped key must not stay registered for the life of the process."""

    await _test_config(oa_app)
    assert active_secret_count() == 0, "the key stayed in the global registry after the call"


async def test_remote_models_config_scopes_and_redacts(oa_app, echoing_provider):
    models = await oa_app.providers.remote_models_config(
        provider_type="openai", base_url="https://example.invalid/v1", api_key=PREFIXLESS
    )
    assert models == []
    assert active_secret_count() == 0


async def test_probe_model_config_scopes_and_redacts(oa_app, echoing_provider):
    probe = await oa_app.providers.probe_model_config(
        model_id="some-model",
        provider_type="openai",
        base_url="https://example.invalid/v1",
        api_key=PREFIXLESS,
    )
    assert PREFIXLESS not in (probe.detail or "")
    assert active_secret_count() == 0


async def test_a_bad_base_url_also_releases_the_key(oa_app, echoing_provider):
    """The early ValueError return path must not skip the release."""

    await oa_app.providers.test_config(
        provider_type="openai", base_url="not-a-url", api_key=PREFIXLESS
    )
    assert active_secret_count() == 0
