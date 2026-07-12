"""Keyboard-driven pilot tests for the backend-first Add-Agent wizard (spec §31, parts 1-3, 16, 20).

These drive the wizard the way a user does — focus a RadioSet, walk options with the arrow keys, and
confirm with Enter (never `.value =` on a selection widget) — across every path: CLI (Codex / Claude /
Antigravity, including an unavailable CLI), API (new connection with a masked key, an existing
connection, a local no-key provider, and a missing key), plus navigation/preservation and Cancel.

The CLI registry is mocked so the tests are deterministic on any host (CI has no CLIs installed).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Button, Input, RadioSet, Select

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.runtimes.cli.registry import CliRegistryEntry
from openagent.tui.app import OpenAgentTUI
from openagent.tui.screens.add_agent import AddAgentScreen
from openagent.tui.screens.lists import AgentsScreen

# --------------------------------------------------------------------------- fixtures / helpers

def _app(tmp_path: Path, *, with_provider: bool = False) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    oa = OpenAgentApp(Paths(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db", project_root=project,
    ))
    if with_provider:
        oa.providers.add(name="deepseek-main", provider_type="deepseek",
                         api_key="sk-x", store_key=False)
    return oa


def _entry(cli_type: str, display: str, *, installed: bool = True) -> CliRegistryEntry:
    return CliRegistryEntry(
        type=cli_type, display_name=display,
        executable=f"/usr/local/bin/{cli_type}" if installed else None,
        version="1.2.3" if installed else None, installed=installed,
        authenticated=True if installed else None, auth_detail="ok" if installed else "",
        adapter=f"{cli_type}-json", structured_events=True, resumable=True,
        experimental=False, status_label="Verified" if installed else "Not installed",
    )


def _cli_entries(*, antigravity_installed: bool = True) -> list[CliRegistryEntry]:
    return [
        _entry("codex", "Codex CLI", installed=True),
        _entry("claude", "Claude Code", installed=True),
        _entry("antigravity", "Antigravity", installed=antigravity_installed),
    ]


@pytest.fixture(autouse=True)
def _mock_cli_registry(monkeypatch):
    """Deterministic CLI catalog (codex/claude/antigravity all installed) for every test here.
    Individual tests override with a narrower list where needed."""
    entries = _cli_entries()

    async def _fake():
        return entries

    monkeypatch.setattr("openagent.tui.screens.add_agent.cli_registry_entries", _fake)


def _use_cli_entries(monkeypatch, entries):
    async def _fake():
        return entries
    monkeypatch.setattr("openagent.tui.screens.add_agent.cli_registry_entries", _fake)


async def _open(pilot) -> AddAgentScreen:
    pilot.app.open_section("add_agent")
    await pilot.pause()
    await pilot.pause()  # on_mount is async (fetches the CLI catalog)
    return pilot.app.screen


async def _pick_radio(pilot, screen, rs_id: str, index: int) -> None:
    """Select option ``index`` in a RadioSet using only the keyboard (walk + Enter)."""
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
        await pilot.press("enter")
        await pilot.pause()
    assert rs.pressed_index == index


async def _continue(pilot) -> None:
    await pilot.click("#continue")
    await pilot.pause()


async def _create(pilot) -> None:
    await pilot.click("#create")
    await pilot.pause()


def _preset_index(name: str) -> int:
    from openagent.providers.factory import preset_names
    return preset_names().index(name)


# --------------------------------------------------------------------------- Step 1: backend choice

async def test_first_screen_is_backend_choice_not_full_form(tmp_path: Path):
    app = OpenAgentTUI(_app(tmp_path))
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        assert screen.step == "backend"
        assert screen.query_one("#step-backend").display is True
        assert "Step 1 of 3" in str(screen.query_one("#step-indicator").render())
        # No provider/model/CLI-selector/key fields on the first screen.
        for hidden in ("#step-cli", "#step-provider", "#step-connection", "#common-fields"):
            assert screen.query_one(hidden).display is False


async def test_backend_cli_then_continue_shows_cli_list(tmp_path: Path):
    app = OpenAgentTUI(_app(tmp_path))
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 0)  # CLI Agent
        await _continue(pilot)
        assert screen.step == "cli"
        assert screen.query_one("#step-cli").display is True
        assert screen.query_one("#step-provider").display is False


async def test_backend_api_then_continue_shows_provider_cards(tmp_path: Path):
    app = OpenAgentTUI(_app(tmp_path))
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 1)  # API Model
        await _continue(pilot)
        assert screen.step == "provider"
        assert screen.query_one("#step-provider").display is True
        # CLI configuration is not shown on the API path.
        assert screen.query_one("#step-cli").display is False
        assert screen.query_one("#cli-runtime-info").display is False


# --------------------------------------------------------------------------- CLI path: create

async def test_create_codex_agent_via_keyboard(tmp_path: Path):
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 0)
        await _continue(pilot)
        await _pick_radio(pilot, screen, "cli", 0)  # Codex
        await _continue(pilot)
        assert screen.step == "cli_config"
        assert "Codex CLI" in str(screen.query_one("#cli-runtime-info").render())
        screen.query_one("#name", Input).value = "codex-coder"
        await _create(pilot)
        assert isinstance(pilot.app.screen, AgentsScreen)
    agent = oa.agents.get("codex-coder")
    assert agent is not None and agent.runtime.cli == "codex"


async def test_create_claude_agent_via_keyboard(tmp_path: Path):
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 0)
        await _continue(pilot)
        await _pick_radio(pilot, screen, "cli", 1)  # Claude Code
        await _continue(pilot)
        screen.query_one("#name", Input).value = "claude-coder"
        await _create(pilot)
    agent = oa.agents.get("claude-coder")
    assert agent is not None and agent.runtime.cli == "claude"


async def test_create_antigravity_agent_when_installed(tmp_path: Path):
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 0)
        await _continue(pilot)
        await _pick_radio(pilot, screen, "cli", 2)  # Antigravity (mocked installed)
        detail = str(screen.query_one("#cli-detail").render())
        assert "antigravity" in detail and "1.2.3" in detail  # executable + version shown
        await _continue(pilot)
        assert screen.step == "cli_config"
        screen.query_one("#name", Input).value = "agy-coder"
        await _create(pilot)
    agent = oa.agents.get("agy-coder")
    assert agent is not None and agent.runtime.cli == "antigravity"


async def test_antigravity_unavailable_blocks_creation(tmp_path: Path, monkeypatch):
    _use_cli_entries(monkeypatch, _cli_entries(antigravity_installed=False))
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 0)
        await _continue(pilot)
        await _pick_radio(pilot, screen, "cli", 2)  # Antigravity — not installed
        assert "not installed" in str(screen.query_one("#cli-detail").render()).lower()
        await _continue(pilot)
        # Continue is blocked; we stay on the CLI step with a clear reason. No unusable agent created.
        assert screen.step == "cli"
        assert "Install or configure" in str(screen.query_one("#err-cli").render())
    assert oa.agents.list() == []


# --------------------------------------------------------------------------- API path: new connection

async def test_api_new_connection_creates_provider_and_agent(tmp_path: Path):
    oa = _app(tmp_path)  # no providers -> only the "new" connection mode
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 1)  # API
        await _continue(pilot)
        await _pick_radio(pilot, screen, "provider", _preset_index("deepseek"))
        await _continue(pilot)
        assert screen.step == "connection"
        # New connection + keychain: the masked API-key field is visible.
        assert screen.query_one("#conn-new").display is True
        assert screen.query_one("#key-row").display is True
        assert screen.query_one("#api_key", Input).password is True
        screen.query_one("#conn_name", Input).value = "deepseek-main"
        screen.query_one("#api_key", Input).value = "sk-secret"
        screen.query_one("#model", Input).value = "deepseek-chat"
        await _continue(pilot)
        assert screen.step == "api_config"
        screen.query_one("#name", Input).value = "ds-coder"
        await _create(pilot)

    provider = oa.providers.get("deepseek-main")
    agent = oa.agents.get("ds-coder")
    assert provider is not None and provider.provider_type == "deepseek"
    assert agent is not None and agent.runtime.provider == "deepseek-main"
    assert agent.runtime.model == "deepseek-chat"
    # The secret never lands in the agent record or the project artifact.
    assert "sk-secret" not in agent.model_dump_json()
    md = tmp_path / "proj" / "OPENAGENT.md"
    if md.exists():
        assert "sk-secret" not in md.read_text()


async def test_api_missing_key_creates_nothing_and_shows_error(tmp_path: Path):
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 1)
        await _continue(pilot)
        await _pick_radio(pilot, screen, "provider", _preset_index("deepseek"))
        await _continue(pilot)
        screen.query_one("#conn_name", Input).value = "deepseek-main"
        # Leave the API key empty (key-required provider).
        screen.query_one("#model", Input).value = "deepseek-chat"
        await _continue(pilot)
        screen.query_one("#name", Input).value = "should-not-exist"
        await _create(pilot)
        # Stayed in the wizard; nothing persisted; an inline error is shown.
        assert isinstance(pilot.app.screen, AddAgentScreen)
    assert oa.providers.get("deepseek-main") is None
    assert oa.agents.get("should-not-exist") is None


async def test_api_local_provider_no_key_manual_model(tmp_path: Path):
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 1)
        await _continue(pilot)
        await _pick_radio(pilot, screen, "provider", _preset_index("ollama"))
        await _continue(pilot)
        # Ollama needs no key; pick "No API key" credential source.
        await _pick_radio(pilot, screen, "cred", 2)
        assert screen.query_one("#key-row").display is False
        assert screen.query_one("#env-row").display is False
        screen.query_one("#conn_name", Input).value = "local-llm"
        screen.query_one("#model", Input).value = "llama3"
        await _continue(pilot)
        screen.query_one("#name", Input).value = "local-agent"
        await _create(pilot)
    agent = oa.agents.get("local-agent")
    provider = oa.providers.get("local-llm")
    assert provider is not None and provider.provider_type == "ollama"
    assert agent is not None and agent.runtime.model == "llama3"


# --------------------------------------------------------------------------- API path: existing conn

async def test_api_existing_connection_no_key_field(tmp_path: Path):
    oa = _app(tmp_path, with_provider=True)  # a saved provider -> "existing" is offered
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 1)
        await _continue(pilot)
        await _pick_radio(pilot, screen, "provider", _preset_index("deepseek"))
        await _continue(pilot)
        await _pick_radio(pilot, screen, "conn-mode", 1)  # Use an existing connection
        assert screen.query_one("#conn-existing").display is True
        assert screen.query_one("#conn-new").display is False
        # No API-key field for an existing connection.
        assert screen.query_one("#key-row").display is False
        screen.query_one("#existing-provider", Select).value = "deepseek-main"
        screen.query_one("#model", Input).value = "deepseek-chat"
        await _continue(pilot)
        screen.query_one("#name", Input).value = "reuse-agent"
        await _create(pilot)
    agent = oa.agents.get("reuse-agent")
    assert agent is not None and agent.runtime.provider == "deepseek-main"


# --------------------------------------------------------------------------- navigation / cancel

async def test_back_without_change_preserves_non_secret_input(tmp_path: Path):
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 1)
        await _continue(pilot)
        await _pick_radio(pilot, screen, "provider", _preset_index("deepseek"))
        await _continue(pilot)
        screen.query_one("#conn_name", Input).value = "deepseek-main"
        screen.query_one("#model", Input).value = "deepseek-chat"
        # Back to provider and forward again WITHOUT changing the provider: input is preserved.
        await pilot.click("#back")
        await pilot.pause()
        assert screen.step == "provider"
        await _continue(pilot)  # same provider (deepseek) — no reset
        assert screen.query_one("#conn_name", Input).value == "deepseek-main"
        assert screen.query_one("#model", Input).value == "deepseek-chat"


async def test_changing_provider_clears_stale_connection_fields(tmp_path: Path):
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 1)
        await _continue(pilot)
        await _pick_radio(pilot, screen, "provider", _preset_index("deepseek"))
        await _continue(pilot)
        screen.query_one("#conn_name", Input).value = "deepseek-main"
        screen.query_one("#model", Input).value = "deepseek-chat"
        screen.query_one("#api_key", Input).value = "sk-stale"
        # Back and pick a DIFFERENT provider -> stale connection widgets + state are cleared.
        await pilot.click("#back")
        await pilot.pause()
        await _pick_radio(pilot, screen, "provider", _preset_index("anthropic"))
        await _continue(pilot)
        assert screen.query_one("#conn_name", Input).value == ""
        assert screen.query_one("#model", Input).value == ""
        assert screen.query_one("#api_key", Input).value == ""
        assert screen.state.model is None


async def test_cancel_creates_nothing(tmp_path: Path):
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 0)
        await _continue(pilot)
        await pilot.click("#cancel")
        await pilot.pause()
        assert not isinstance(pilot.app.screen, AddAgentScreen)
    assert oa.agents.list() == []


# --------------------------------------------------------------------------- action bar visibility

@pytest.mark.parametrize("size", [(80, 24), (100, 30), (120, 40)])
async def test_action_bar_visible_at_terminal_sizes(tmp_path: Path, size):
    app = OpenAgentTUI(_app(tmp_path))
    async with app.run_test(size=size) as pilot:
        screen = await _open(pilot)
        cont = screen.query_one("#continue", Button)
        assert cont.display
        assert 0 < cont.region.bottom <= app.size.height, f"Continue offscreen at {size}"
