"""NVIDIA Build in the Add-Agent wizard (spec §13, §14, §15).

Keyboard/pilot-driven, like the rest of the wizard suite. These prove the honest bits: the card is
visible and describes the hosted catalog, the connection step is provider-aware (fixed endpoint +
protocol, keychain recommended, env var pre-filled, no "no key" option), the catalog browser filters
locally, the mixed-catalog warning is shown, and an unprobed model cannot silently become an agent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from textual.widgets import Checkbox, Input, RadioButton, RadioSet, Select

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import ModelCapabilities, RemoteModel
from openagent.providers.discovery import (
    PROBE_PARTIAL,
    PROBE_VERIFIED,
    AgentModelProbe,
)
from openagent.providers.factory import preset_names
from openagent.tui.app import OpenAgentTUI
from openagent.tui.screens.add_agent import AddAgentScreen
from tests.tui_helpers import select_all_option_values

_CATALOG = [
    RemoteModel(id="nvidia/nemotron-test", display_name="nvidia/nemotron-test", owned_by="nvidia"),
    RemoteModel(id="nvidia/embed-test", display_name="nvidia/embed-test", owned_by="nvidia"),
    RemoteModel(id="meta/vision-test", display_name="meta/vision-test", owned_by="meta"),
    RemoteModel(
        id="deepseek-ai/chat-test", display_name="deepseek-ai/chat-test", owned_by="deepseek-ai"
    ),
]


def _app(tmp_path: Path) -> OpenAgentApp:
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


@pytest.fixture(autouse=True)
def _mock_cli_registry(monkeypatch):
    async def _fake():
        return []

    monkeypatch.setattr("openagent.tui.screens.add_agent.cli_registry_entries", _fake)


@pytest.fixture(autouse=True)
def _mock_catalog(monkeypatch):
    async def _models_config(self, **kwargs):  # noqa: ANN001
        return _CATALOG

    monkeypatch.setattr(
        "openagent.services.provider_service.ProviderService.remote_models_config", _models_config
    )


def _nvidia_index() -> int:
    return preset_names().index("nvidia-build")


async def _open(pilot) -> AddAgentScreen:
    pilot.app.open_section("add_agent")
    await pilot.pause()
    await pilot.pause()
    return pilot.app.screen


async def _pick_radio(pilot, screen, rs_id: str, index: int) -> None:
    rs = screen.query_one(f"#{rs_id}", RadioSet)
    screen.set_focus(rs)
    await pilot.pause()
    cur = rs._selected if rs._selected is not None else 0
    delta = index - cur
    key = "down" if delta > 0 else "up"
    for _ in range(abs(delta)):
        await pilot.press(key)
        await pilot.pause()
    if rs.pressed_index != index:
        await pilot.press("space")
        await pilot.pause()
    assert rs.pressed_index == index


async def _continue(pilot) -> None:
    await pilot.click("#continue")
    await pilot.pause()


async def _to_connection(pilot) -> AddAgentScreen:
    """Backend → API → NVIDIA Build → Connection."""

    screen = await _open(pilot)
    await _pick_radio(pilot, screen, "backend", 1)  # API Model
    await _continue(pilot)
    assert screen.step == "provider"
    await _pick_radio(pilot, screen, "provider", _nvidia_index())
    await _continue(pilot)
    assert screen.step == "connection"
    return screen


def _probe(model: str, *, verified: bool) -> AgentModelProbe:
    caps = ModelCapabilities(text=True, streaming=True, tool_calling=True if verified else None)
    return AgentModelProbe(
        model,
        caps,
        verified,
        PROBE_VERIFIED if verified else PROBE_PARTIAL,
        "",
        datetime.now(timezone.utc),
    )


# --------------------------------------------------------------------------- §13 provider card


async def test_nvidia_card_is_visible_and_describes_the_hosted_catalog(tmp_path: Path):
    async with OpenAgentTUI(_app(tmp_path)).run_test(size=(120, 60)) as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 1)
        await _continue(pilot)
        # The card exists in the provider list…
        button = screen.query_one(f"#preset-{'nvidia-build'}", RadioButton)
        assert "NVIDIA Build (Hosted NIM APIs)" in str(button.label)
        # …and selecting it explains what it is (§13).
        await _pick_radio(pilot, screen, "provider", _nvidia_index())
        detail = str(screen.query_one("#provider-detail").render())
        assert "Hosted NVIDIA NIM endpoints from build.nvidia.com." in detail
        assert "Use one NVIDIA API key to access available catalog models." in detail
        assert "https://integrate.api.nvidia.com/v1" in detail


async def test_connection_step_is_provider_aware(tmp_path: Path):
    async with OpenAgentTUI(_app(tmp_path)).run_test(size=(120, 60)) as pilot:
        screen = await _to_connection(pilot)
        info = str(screen.query_one("#hosted-info").render())
        # The official endpoint and protocol are shown as fixed facts (§13.1).
        assert "https://integrate.api.nvidia.com/v1" in info
        assert "openai-chat" in info
        # Defaults: connection name + env var name (§13.1).
        assert screen.query_one("#conn_name", Input).value == "nvidia-build"
        assert screen.query_one("#key_env", Input).value == "NVIDIA_API_KEY"
        # "No API key" is never offered for a provider that requires one (§13.1).
        assert screen.query_one("#cred-none", RadioButton).display is False
        # Key instructions are shown (§13.5).
        hint = str(screen.query_one("#hosted-hint").render())
        assert "Generate API Key" in hint
        assert "Never put the key directly in a command." in hint
        assert "nvapi-" in hint  # a hint, not a validation rule


async def test_keychain_input_is_masked_and_cleared_on_unmount(tmp_path: Path):
    async with OpenAgentTUI(_app(tmp_path)).run_test(size=(120, 60)) as pilot:
        screen = await _to_connection(pilot)
        key_input = screen.query_one("#api_key", Input)
        assert key_input.password is True  # hidden entry (§13.2)
        key_input.value = "nvapi-THIS_IS_A_FAKE_TEST_KEY_123456"
        screen._clear_secret_widget()
        assert key_input.value == ""
        assert screen.state.api_key is None


async def test_open_nvidia_build_uses_webbrowser_not_a_shell(tmp_path: Path, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(
        "openagent.tui.screens.add_agent.webbrowser.open", lambda url: opened.append(url) or True
    )
    async with OpenAgentTUI(_app(tmp_path)).run_test(size=(120, 60)) as pilot:
        await _to_connection(pilot)
        await pilot.click("#open-catalog")
        await pilot.pause()
    assert opened == ["https://build.nvidia.com/"]


async def test_missing_env_var_is_an_explicit_connection_error(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    async with OpenAgentTUI(_app(tmp_path)).run_test(size=(120, 60)) as pilot:
        screen = await _to_connection(pilot)
        await _pick_radio(pilot, screen, "cred", 1)  # Environment variable
        await _continue(pilot)
        # It must not advance, and must say exactly what is wrong (§13.3).
        assert screen.step == "connection"
        err = str(screen.query_one("#err-conn").render())
        assert "NVIDIA_API_KEY is not set" in err


# --------------------------------------------------------------------------- §14 catalog browser


async def _to_model_step(pilot, monkeypatch) -> AddAgentScreen:
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-THIS_IS_A_FAKE_TEST_KEY_123456")
    screen = await _to_connection(pilot)
    await _pick_radio(pilot, screen, "cred", 1)  # env var (no keychain write in a test)
    await _continue(pilot)
    assert screen.step == "model"
    await pilot.click("#model-refresh")
    await pilot.pause()
    await pilot.pause()
    return screen


async def test_catalog_loads_with_publisher_filter_and_mixed_warning(tmp_path: Path, monkeypatch):
    async with OpenAgentTUI(_app(tmp_path)).run_test(size=(120, 60)) as pilot:
        screen = await _to_model_step(pilot, monkeypatch)
        values = [
            v
            for v in select_all_option_values(screen.query_one("#model_select", Select))
            if isinstance(v, str)
        ]
        assert values == [m.id for m in _CATALOG]
        # The publisher filter is populated from what the catalog reported (§14.2).
        owners = [
            v
            for v in select_all_option_values(screen.query_one("#model-owner", Select))
            if isinstance(v, str)
        ]
        assert owners == ["deepseek-ai", "meta", "nvidia"]
        # The mixed-catalog warning is explicit (§14.3).
        warning = str(screen.query_one("#catalog-warning").render())
        assert "not automatically compatible" in warning
        assert "embedding" in warning


async def test_search_filters_locally(tmp_path: Path, monkeypatch):
    async with OpenAgentTUI(_app(tmp_path)).run_test(size=(120, 60)) as pilot:
        screen = await _to_model_step(pilot, monkeypatch)
        screen.query_one("#model-search", Input).value = "nemotron"
        await pilot.pause()
        values = [
            v
            for v in select_all_option_values(screen.query_one("#model_select", Select))
            if isinstance(v, str)
        ]
        assert values == ["nvidia/nemotron-test"]


async def test_non_chat_entries_are_labelled_as_a_hint_only(tmp_path: Path, monkeypatch):
    async with OpenAgentTUI(_app(tmp_path)).run_test(size=(120, 60)) as pilot:
        screen = await _to_model_step(pilot, monkeypatch)
        labels = [str(o[0]) for o in screen.query_one("#model_select", Select)._options]
        embed = next(x for x in labels if "embed-test" in x)
        assert "may not be a chat model" in embed
        # …but it is still selectable: only a probe may decide (§14.3).
        values = [
            v
            for v in select_all_option_values(screen.query_one("#model_select", Select))
            if isinstance(v, str)
        ]
        assert "nvidia/embed-test" in values


# --------------------------------------------------------------------------- §15 validation gate


async def test_unprobed_model_cannot_advance(tmp_path: Path, monkeypatch):
    async with OpenAgentTUI(_app(tmp_path)).run_test(size=(120, 60)) as pilot:
        screen = await _to_model_step(pilot, monkeypatch)
        screen.query_one("#model_select", Select).value = "nvidia/nemotron-test"
        await pilot.pause()
        await _continue(pilot)
        # Blocked, with the exact remedy (§14.3 / §15).
        assert screen.step == "model"
        err = str(screen.query_one("#err-model").render())
        assert "has not been validated" in err


async def test_validate_marks_a_verified_model_and_allows_advancing(tmp_path: Path, monkeypatch):
    async def _probe_config(self, *, model_id, **kwargs):  # noqa: ANN001
        return _probe(model_id, verified=True)

    monkeypatch.setattr(
        "openagent.services.provider_service.ProviderService.probe_model_config", _probe_config
    )
    async with OpenAgentTUI(_app(tmp_path)).run_test(size=(120, 60)) as pilot:
        screen = await _to_model_step(pilot, monkeypatch)
        screen.query_one("#model_select", Select).value = "nvidia/nemotron-test"
        await pilot.pause()
        await pilot.click("#model-validate")
        await pilot.pause()
        await pilot.pause()
        verify = str(screen.query_one("#model-verify").render())
        assert "Verified Agent Compatible" in verify
        await _continue(pilot)
        assert screen.step == "details"


async def test_partial_model_is_blocked_but_can_be_overridden_explicitly(
    tmp_path: Path, monkeypatch
):
    async def _probe_config(self, *, model_id, **kwargs):  # noqa: ANN001
        return _probe(model_id, verified=False)

    monkeypatch.setattr(
        "openagent.services.provider_service.ProviderService.probe_model_config", _probe_config
    )
    async with OpenAgentTUI(_app(tmp_path)).run_test(size=(120, 60)) as pilot:
        screen = await _to_model_step(pilot, monkeypatch)
        screen.query_one("#model_select", Select).value = "nvidia/embed-test"
        await pilot.pause()
        await pilot.click("#model-validate")
        await pilot.pause()
        await pilot.pause()
        verify = str(screen.query_one("#model-verify").render())
        assert "tool calling was not verified" in verify

        # A partial model is blocked by default…
        await _continue(pilot)
        assert screen.step == "model"

        # …and the override is present but NOT pre-selected (§15.3).
        override = screen.query_one("#allow-unverified", Checkbox)
        assert override.value is False
        await pilot.click("#allow-unverified")
        await pilot.pause()
        await pilot.pause()
        assert override.value is True
        screen.query_one("#override-reason", Input).value = "manual compatibility review"
        await _continue(pilot)
        assert screen.step == "details"


async def test_review_warns_loudly_for_an_unverified_model(tmp_path: Path, monkeypatch):
    async with OpenAgentTUI(_app(tmp_path)).run_test(size=(120, 60)) as pilot:
        screen = await _to_model_step(pilot, monkeypatch)
        screen.query_one("#model_select", Select).value = "nvidia/nemotron-test"
        await pilot.pause()
        await pilot.click("#allow-unverified")
        await pilot.pause()
        await pilot.pause()
        screen.query_one("#override-reason", Input).value = "manual compatibility review"
        await _continue(pilot)
        assert screen.step == "details"
        screen.query_one("#name", Input).value = "nvidia-coder"
        await _continue(pilot)
        assert screen.step == "review"
        card = str(screen.query_one("#review-card").render())
        assert "NOT verified agent-compatible" in card
        assert "WARNING" in card
