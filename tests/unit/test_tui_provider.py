"""Pilot tests for provider management in the TUI (spec §31, item 3)."""

from __future__ import annotations

from pathlib import Path

from textual.widgets import Input, Select

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.tui.app import OpenAgentTUI
from openagent.tui.screens.add_agent import AddAgentScreen
from openagent.tui.screens.add_provider import AddProviderScreen


def _app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    paths = Paths(data_dir=tmp_path / "data", config_dir=tmp_path / "config",
                  db_path=tmp_path / "data" / "openagent.db", project_root=project)
    return OpenAgentApp(paths)


async def test_credential_source_toggles_key_and_env_fields(tmp_path: Path):
    app = OpenAgentTUI(_app(tmp_path))
    async with app.run_test() as pilot:
        pilot.app.push_screen(AddProviderScreen())
        await pilot.pause()
        screen = pilot.app.screen
        # Default keychain → masked key field visible, env field hidden.
        assert screen.query_one("#key-row").display is True
        assert screen.query_one("#env-row").display is False
        assert screen.query_one("#api_key", Input).password is True

        screen.query_one("#cred", Select).value = "env"
        await pilot.pause()
        assert screen.query_one("#env-row").display is True
        assert screen.query_one("#key-row").display is False

        screen.query_one("#cred", Select).value = "none"
        await pilot.pause()
        assert screen.query_one("#key-row").display is False
        assert screen.query_one("#env-row").display is False


async def test_save_provider_then_available_in_add_agent(tmp_path: Path):
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        pilot.app.push_screen(AddProviderScreen())
        await pilot.pause()
        screen = pilot.app.screen
        screen.query_one("#name", Input).value = "deepseek-main"
        screen.query_one("#preset", Select).value = "deepseek"
        screen.query_one("#cred", Select).value = "none"
        await pilot.pause()
        screen.action_save()
        await pilot.pause()

    # Persisted…
    assert oa.providers.get("deepseek-main") is not None

    # …and immediately selectable in the Add Agent form.
    app2 = OpenAgentTUI(oa)
    async with app2.run_test() as pilot:
        pilot.app.open_section("add_agent")
        await pilot.pause()
        add = pilot.app.screen
        assert isinstance(add, AddAgentScreen)
        provider_values = [opt[1] for opt in add.query_one("#provider", Select)._options  # type: ignore[attr-defined]
                           if opt[1] is not None]
        assert "deepseek-main" in provider_values


async def test_saved_key_never_displayed(tmp_path: Path):
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        pilot.app.push_screen(AddProviderScreen())
        await pilot.pause()
        screen = pilot.app.screen
        # The key input is a password field, so its rendered content is masked.
        screen.query_one("#api_key", Input).value = "sk-supersecret-123456"
        assert screen.query_one("#api_key", Input).password is True
