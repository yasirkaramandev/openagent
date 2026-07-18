from __future__ import annotations

import re
from pathlib import Path

import pytest
from textual.events import MouseScrollDown, MouseScrollUp
from textual.widgets import Button

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import RuntimeType
from openagent.tui.app import OpenAgentTUI
from openagent.tui.screens.add_agent import AddAgentScreen
from openagent.tui.screens.add_provider import AddProviderScreen
from openagent.tui.screens.doctor import DoctorScreen
from openagent.tui.screens.lists import AgentsScreen
from openagent.tui.screens.modals import QuestionModal
from openagent.tui.screens.run_console import RunConsoleScreen, RunSetupScreen

SIZES = [(120, 40), (100, 30), (80, 24), (70, 20), (60, 18), (50, 14), (40, 12)]
SNAPSHOTS = Path(__file__).resolve().parents[1] / "snapshots" / "tui"


def _oa(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "project"
    # Idempotent: some tests build the app twice under one tmp_path to compare two renders.
    project.mkdir(exist_ok=True)
    app = OpenAgentApp(
        Paths(
            data_dir=tmp_path / "data",
            config_dir=tmp_path / "config",
            db_path=tmp_path / "data" / "openagent.db",
            project_root=project,
        )
    )
    app.agents.create(name="codex", runtime_type=RuntimeType.CLI, cli="codex")
    return app


def _assert_visible(app: OpenAgentTUI, button: Button) -> None:
    assert button.display
    assert button.region.height > 0
    assert 0 <= button.region.y < app.size.height
    assert 0 <= button.region.x < app.size.width
    assert button.region.right <= app.size.width
    assert button.region.bottom <= app.size.height


@pytest.mark.parametrize("size", SIZES)
async def test_full_terminal_matrix_keeps_forms_and_modal_actions_visible(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    app = OpenAgentTUI(_oa(tmp_path))
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert app.has_class("narrow") is (size[0] < 80)
        assert app.has_class("tiny") is (size[0] < 60)
        _assert_visible(app, app.screen.query_one("#dash-quit", Button))

        app.push_screen(AddProviderScreen())
        await pilot.pause()
        provider = app.screen
        _assert_visible(app, provider.query_one("#save", Button))
        _assert_visible(app, provider.query_one("#cancel", Button))
        form = provider.query_one("#form")
        form.post_message(MouseScrollDown(form, 1, 1, 0, 1, 0, False, False, False))
        await pilot.pause()
        assert form.scroll_y > 0
        form.post_message(MouseScrollUp(form, 1, 1, 0, -1, 0, False, False, False))
        await pilot.pause()
        for key in ("tab", "tab", "pagedown", "pageup", "home", "end"):
            await pilot.press(key)
        assert app.focused is not None
        assert app.focused.region.bottom <= app.size.height

        app.push_screen(QuestionModal("[green]fake approval[/green] " + ("long question " * 80)))
        await pilot.pause()
        modal = app.screen
        _assert_visible(app, modal.query_one("#ok", Button))
        _assert_visible(app, modal.query_one("#cancel", Button))
        assert "[green]" in str(modal.query_one("#q").render())
        await pilot.press("pagedown", "home", "escape")
        await pilot.pause()
        app.pop_screen()
        await pilot.pause()


@pytest.mark.parametrize("size", [(120, 40), (80, 24), (40, 12)])
async def test_critical_screen_matrix_has_scroll_body_and_fixed_action_bar(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    oa = _oa(tmp_path)
    run = oa.runs.create(agent_name="codex", prompt="responsive test")
    app = OpenAgentTUI(oa)
    screens = [
        AgentsScreen(),
        RunSetupScreen(),
        RunConsoleScreen(run.id),
        AddAgentScreen(),
        DoctorScreen(),
    ]

    async with app.run_test(size=size) as pilot:
        for screen in screens:
            app.push_screen(screen)
            await pilot.pause()
            footer = screen.query_one("Footer")
            assert footer.region.bottom <= app.size.height
            action_bars = list(screen.query(".action-bar"))
            assert action_bars, f"{screen.__class__.__name__} has no fixed action bar"
            assert action_bars[-1].region.bottom <= footer.region.y
            await pilot.press("tab", "shift+tab", "pagedown", "pageup", "home", "end")
            app.pop_screen()
            await pilot.pause()


def _layout_only(svg: str) -> str:
    """Strip presentation from an exported SVG, keeping geometry and text.

    The snapshot exists to pin **layout** at a given terminal size — where each cell lands, how wide
    it is, what text it holds. Colours and font weights come from the active theme and from Rich's
    SVG exporter, and ``rich`` is an unpinned dependency (``>=13.7``), so a Rich release that
    restyles the footer changes every byte of the file without moving a single character. Comparing
    raw bytes made this gate fail on any machine whose Rich differed from the one that generated the
    file — which is a broken gate, not a caught regression.

    So the comparison drops three things and nothing else:

    * ``terminal-<n>`` — a content hash of the render, so it changes whenever styling does;
    * the ``<style>`` block — pure presentation;
    * ``class`` attributes — style-slot numbers that get renumbered when styling changes;
    * ``fill="#rrggbb"`` — the theme colour painted into each background rect.

    Everything load-bearing (``x``, ``y``, ``width``, ``height``, ``textLength``, ``clip-path`` and
    the text itself) is kept, so a real layout regression still fails.
    """

    svg = re.sub(r"terminal-\d+", "terminal-ID", svg)
    svg = re.sub(r"<style>.*?</style>", "<style/>", svg, flags=re.DOTALL)
    svg = re.sub(r'\sclass="[^"]*"', "", svg)
    svg = re.sub(r'fill="#[0-9a-fA-F]{3,8}"', 'fill="COLOUR"', svg)
    return svg.rstrip()


@pytest.mark.parametrize("size", [(80, 24), (40, 12)])
async def test_add_provider_svg_snapshot_is_deterministic(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    app = OpenAgentTUI(_oa(tmp_path))
    async with app.run_test(size=size) as pilot:
        app.push_screen(AddProviderScreen())
        await pilot.pause()
        title = f"OpenAgent Add Provider {size[0]}x{size[1]}"
        actual = app.export_screenshot(title=title, simplify=True)
        expected = (SNAPSHOTS / f"add_provider_{size[0]}x{size[1]}.svg").read_text()
        assert _layout_only(actual) == _layout_only(expected)


@pytest.mark.parametrize("size", [(80, 24), (40, 12)])
async def test_add_provider_render_is_reproducible_within_a_run(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    """The exporter itself must be deterministic — otherwise the snapshot proves nothing."""

    async def render(slot: str) -> str:
        # A fresh state directory per render: the app seeds an agent into its database, so reusing
        # one would make the second call fail on "already exists" rather than compare two renders.
        root = tmp_path / slot
        root.mkdir()
        app = OpenAgentTUI(_oa(root))
        async with app.run_test(size=size) as pilot:
            app.push_screen(AddProviderScreen())
            await pilot.pause()
            return app.export_screenshot(title="repeat", simplify=True)

    assert await render("first") == await render("second")
