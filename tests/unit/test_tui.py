"""TUI boot + navigation smoke tests via Textual's pilot."""

from pathlib import Path

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import RuntimeType
from openagent.tui.app import OpenAgentTUI
from openagent.tui.screens.doctor import DoctorScreen
from openagent.tui.screens.lists import AgentsScreen


def _make_app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    paths = Paths(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db",
        project_root=project,
    )
    oa = OpenAgentApp(paths)
    oa.agents.create(
        name="codex-coder",
        title="Codex Coder",
        runtime_type=RuntimeType.CLI,
        cli="codex",
        tags=["coder"],
    )
    return oa


async def test_dashboard_boots_and_shows_stats(tmp_path: Path):
    app = OpenAgentTUI(_make_app(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        stats = app.screen.query_one("#stats")
        assert "OpenAgent" in str(stats.render())


async def test_open_agents_and_doctor_sections(tmp_path: Path):
    app = OpenAgentTUI(_make_app(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_section("agents")
        await pilot.pause()
        assert isinstance(app.screen, AgentsScreen)
        table = app.screen.query_one("#table")
        assert table.row_count == 1  # the codex-coder agent

        app.pop_screen()
        await pilot.pause()
        app.open_section("doctor")
        await pilot.pause()
        assert isinstance(app.screen, DoctorScreen)


async def test_add_agent_section_opens(tmp_path: Path):
    app = OpenAgentTUI(_make_app(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_section("add_agent")
        await pilot.pause()
        assert app.screen.query_one("#name") is not None
