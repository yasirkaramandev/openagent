"""Pilot tests for the Add Agent wizard and Agents screen (spec §31, items 1–2).

Verifies conditional fields, the always-visible Create button (at small terminal sizes), CLI and API
agent creation through the UI, visible validation, and that a new agent appears in the Agents list.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Button, Input, Select

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.tui.app import OpenAgentTUI
from openagent.tui.screens.add_agent import AddAgentScreen
from openagent.tui.screens.lists import AgentsScreen


def _app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    paths = Paths(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db", project_root=project,
    )
    oa = OpenAgentApp(paths)
    # A provider so the API path has something to select.
    oa.providers.add(name="deepseek-main", provider_type="deepseek", api_key="sk-x", store_key=False)
    return oa


async def _open_add(pilot) -> AddAgentScreen:
    pilot.app.open_section("add_agent")
    await pilot.pause()
    return pilot.app.screen


@pytest.mark.parametrize("size", [(80, 24), (100, 30), (120, 40)])
async def test_create_button_visible_at_terminal_sizes(tmp_path: Path, size):
    app = OpenAgentTUI(_app(tmp_path))
    async with app.run_test(size=size) as pilot:
        screen = await _open_add(pilot)
        button = screen.query_one("#create", Button)
        assert button.display
        # The fixed action bar keeps the button within the visible viewport.
        assert 0 < button.region.bottom <= app.size.height, f"create button offscreen at {size}"
        assert button.region.width > 0


async def test_cli_fields_appear_only_for_cli_runtime(tmp_path: Path):
    app = OpenAgentTUI(_app(tmp_path))
    async with app.run_test() as pilot:
        screen = await _open_add(pilot)
        # Default is API: API fields shown, CLI hidden.
        assert screen.query_one("#api-group").display is True
        assert screen.query_one("#cli-group").display is False

        screen.query_one("#runtime", Select).value = "cli"
        await pilot.pause()
        assert screen.query_one("#cli-group").display is True
        assert screen.query_one("#api-group").display is False
        # The CLI select is present and offers the known CLIs with an install status label.
        cli_select = screen.query_one("#cli", Select)
        labels = [opt[0] for opt in cli_select._options]  # type: ignore[attr-defined]
        assert any("codex" in label for label in labels)
        assert any("claude" in label for label in labels)


async def test_create_codex_cli_agent_through_tui(tmp_path: Path):
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open_add(pilot)
        screen.query_one("#runtime", Select).value = "cli"
        await pilot.pause()
        screen.query_one("#cli", Select).value = "codex"
        screen.query_one("#name", Input).value = "codex-coder"
        screen.query_one("#title", Input).value = "Codex Coder"
        screen.query_one("#description", Input).value = "does codey things"
        await pilot.click("#create")  # prove the button is actionable
        await pilot.pause()

    agent = oa.agents.get("codex-coder")
    assert agent is not None
    assert agent.runtime.cli == "codex"
    assert agent.description == "does codey things"


async def test_create_claude_cli_agent_through_tui(tmp_path: Path):
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open_add(pilot)
        screen.query_one("#runtime", Select).value = "cli"
        await pilot.pause()
        screen.query_one("#cli", Select).value = "claude"
        screen.query_one("#name", Input).value = "claude-coder"
        screen.action_create()
        await pilot.pause()

    assert oa.agents.get("claude-coder").runtime.cli == "claude"


async def test_create_api_agent_through_tui(tmp_path: Path):
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open_add(pilot)
        screen.query_one("#provider", Select).value = "deepseek-main"
        screen.query_one("#model", Input).value = "deepseek-chat"
        screen.query_one("#name", Input).value = "ds-coder"
        screen.action_create()
        await pilot.pause()

    agent = oa.agents.get("ds-coder")
    assert agent is not None and agent.runtime.provider == "deepseek-main"
    assert agent.runtime.model == "deepseek-chat"


async def test_validation_error_is_visible_and_form_stays_open(tmp_path: Path):
    app = OpenAgentTUI(_app(tmp_path))
    async with app.run_test() as pilot:
        screen = await _open_add(pilot)
        screen.query_one("#runtime", Select).value = "cli"
        await pilot.pause()
        # No name, no CLI chosen → create must fail visibly and not close the form.
        screen.action_create()
        await pilot.pause()
        assert isinstance(pilot.app.screen, AddAgentScreen)
        summary = str(screen.query_one("#error-summary").render())
        assert "Cannot create agent" in summary
        assert "required" in str(screen.query_one("#err-name").render())


async def test_created_agent_appears_in_agents_screen(tmp_path: Path):
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open_add(pilot)
        screen.query_one("#runtime", Select).value = "cli"
        await pilot.pause()
        screen.query_one("#cli", Select).value = "codex"
        screen.query_one("#name", Input).value = "shiny-new"
        screen.action_create()
        await pilot.pause()
        # On success the wizard returns to the Agents list, which now includes the agent.
        assert isinstance(pilot.app.screen, AgentsScreen)
        table = pilot.app.screen.query_one("#table")
        names = [table.get_row_at(i)[0] for i in range(table.row_count)]
        assert "shiny-new" in names


async def test_agents_screen_shows_description_in_details(tmp_path: Path):
    oa = _app(tmp_path)
    from openagent.core.models import RuntimeType

    oa.agents.create(name="documented", title="Doc Agent", description="a very specific purpose",
                     runtime_type=RuntimeType.CLI, cli="codex")
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        pilot.app.open_section("agents")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, AgentsScreen)
        details = str(screen.query_one("#details").render())
        assert "a very specific purpose" in details
