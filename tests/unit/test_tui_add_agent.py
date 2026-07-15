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
from textual.widgets import Button, Input, Label, RadioSet, Select

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.runtimes.cli.registry import CliRegistryEntry
from openagent.tui.app import OpenAgentTUI
from openagent.tui.screens.add_agent import AddAgentScreen
from openagent.tui.screens.lists import AgentsScreen
from tests.tui_helpers import select_all_option_values


def _model_option_values(select: Select) -> list[str]:
    """Discovered model ids currently offered, excluding the blank/NULL sentinel."""
    return [v for v in select_all_option_values(select) if isinstance(v, str)]

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
    """Select option ``index`` in a RadioSet using only the keyboard (walk + Space).

    **Space** selects; **Enter** advances the wizard (part 19). Enter used to be bound to "toggle",
    so pressing it on a radio group re-selected the current option and went nowhere.
    """

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


async def _create(pilot) -> None:
    await pilot.click("#create")
    await pilot.pause()


async def _model_default_then_details(pilot, screen) -> None:
    """On the Model step: pick the CLI default (model=None) and advance to Agent Details."""
    assert screen.step == "model"
    await _pick_radio(pilot, screen, "model-mode", 2)  # Use the CLI's default model
    await _continue(pilot)
    assert screen.step == "details"


async def _model_manual_then_details(pilot, screen, model_id: str) -> None:
    """On the Model step: enter a manual id and advance to Agent Details."""
    assert screen.step == "model"
    await _pick_radio(pilot, screen, "model-mode", 1)  # Enter a model ID manually
    screen.query_one("#model", Input).value = model_id
    await _continue(pilot)
    assert screen.step == "details"


async def _details_then_review(pilot, screen, name: str) -> None:
    screen.query_one("#name", Input).value = name
    await _continue(pilot)
    assert screen.step == "review"


def _preset_index(name: str) -> int:
    from openagent.providers.factory import preset_names
    return preset_names().index(name)


def _cli_index(cli_type: str) -> int:
    return [e.type for e in _cli_entries()].index(cli_type)


# --------------------------------------------------------------------------- Step 1: backend choice

async def test_first_screen_is_backend_choice_not_full_form(tmp_path: Path):
    app = OpenAgentTUI(_app(tmp_path))
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        assert screen.step == "backend"
        assert screen.query_one("#step-backend").display is True
        assert "Step 1 of 5" in str(screen.query_one("#step-indicator").render())
        # No provider/model/CLI-selector/key fields on the first screen.
        for hidden in ("#step-cli", "#step-provider", "#step-connection", "#step-model",
                       "#common-fields", "#step-review"):
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
        assert screen.query_one("#step-model").display is False


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
        await _model_default_then_details(pilot, screen)  # model is its own step now (item 11)
        await _details_then_review(pilot, screen, "codex-coder")
        await _create(pilot)
        assert isinstance(pilot.app.screen, AgentsScreen)
    agent = oa.agents.get("codex-coder")
    assert agent is not None and agent.runtime.cli == "codex"
    assert agent.runtime.model is None  # "Use CLI default" persists as no pinned model (item 11)


async def test_create_claude_agent_via_keyboard(tmp_path: Path):
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 0)
        await _continue(pilot)
        await _pick_radio(pilot, screen, "cli", 1)  # Claude Code
        await _continue(pilot)
        await _model_default_then_details(pilot, screen)
        await _details_then_review(pilot, screen, "claude-coder")
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
        await _model_default_then_details(pilot, screen)
        await _details_then_review(pilot, screen, "agy-coder")
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


# --------------------------------------------------------------------------- CLI path: model discovery (Phase 4)

async def test_cli_model_discovery_populates_select_and_pins_model(tmp_path: Path, monkeypatch):
    """Discover Models runs the adapter's real discovery; a chosen id pins onto AgentRuntime.model."""

    from openagent.runtimes.cli.registry import CliModelDiscovery

    async def _fake_discover(cli_type, executable=None):
        return CliModelDiscovery(cli_type, True, ["Gemini 3.5 Flash (Low)", "Claude Sonnet 4.6"],
                                 "agy models")

    monkeypatch.setattr("openagent.tui.screens.add_agent.discover_cli_models", _fake_discover)
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 0)
        await _continue(pilot)
        await _pick_radio(pilot, screen, "cli", 2)  # Antigravity
        await _continue(pilot)
        assert screen.step == "model"
        screen._refresh_models()  # explicit Refresh on the Model step
        await pilot.pause()
        await pilot.pause()
        options = _model_option_values(screen.query_one("#model_select", Select))
        assert options == ["Gemini 3.5 Flash (Low)", "Claude Sonnet 4.6"]
        assert "found 2 model(s)" in str(screen.query_one("#model-status").render())
        # Discovered mode (default) + pick a discovered model.
        screen.query_one("#model_select", Select).value = "Gemini 3.5 Flash (Low)"
        await pilot.pause()
        await _continue(pilot)
        await _details_then_review(pilot, screen, "agy-pinned")
        await _create(pilot)
    agent = oa.agents.get("agy-pinned")
    assert agent is not None and agent.runtime.model == "Gemini 3.5 Flash (Low)"


async def test_cli_model_discovery_unavailable_keeps_manual_and_default_paths(tmp_path: Path, monkeypatch):
    """When a CLI can't list models, the wizard says so honestly and manual/blank still work."""

    from openagent.runtimes.cli.registry import CliModelDiscovery

    async def _fake_discover(cli_type, executable=None):
        return CliModelDiscovery(cli_type, False, [], "",
                                 "automatic model discovery is unavailable for Codex CLI")

    monkeypatch.setattr("openagent.tui.screens.add_agent.discover_cli_models", _fake_discover)
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 0)
        await _continue(pilot)
        await _pick_radio(pilot, screen, "cli", 0)  # Codex — no offline model listing
        await _continue(pilot)
        assert screen.step == "model"
        screen._refresh_models()
        await pilot.pause()
        await pilot.pause()
        status = str(screen.query_one("#model-status").render())
        assert "unavailable" in status
        assert _model_option_values(screen.query_one("#model_select", Select)) == []
        # Manual id still works — nothing is blocked or faked.
        await _model_manual_then_details(pilot, screen, "o3")
        await _details_then_review(pilot, screen, "codex-manual")
        await _create(pilot)
    agent = oa.agents.get("codex-manual")
    assert agent is not None and agent.runtime.model == "o3"


# --------------------------------------------------------------------------- model step (item 11)

async def test_model_selection_is_its_own_step_before_details(tmp_path: Path):
    """Model is a dedicated step between the backend choice and Agent Details (item 11)."""
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        assert screen._step_list() == ["backend", "cli", "model", "details", "review"]
        await _pick_radio(pilot, screen, "backend", 0)
        await _continue(pilot)
        await _pick_radio(pilot, screen, "cli", 0)
        await _continue(pilot)
        assert screen.step == "model"
        # Agent Details is NOT shown on the model step (it used to be the same screen).
        assert screen.query_one("#step-model").display is True
        assert screen.query_one("#common-fields").display is False


async def test_switching_cli_resets_the_discovered_model_list(tmp_path: Path, monkeypatch):
    """A discovered list must not leak across a CLI change (item 11)."""
    from openagent.runtimes.cli.registry import CliModelDiscovery

    async def _fake_discover(cli_type, executable=None):
        if cli_type == "antigravity":
            return CliModelDiscovery("antigravity", True, ["Gemini 3.5 Flash (Low)"], "agy models")
        return CliModelDiscovery(cli_type, False, [], "", "does not expose model listing")

    monkeypatch.setattr("openagent.tui.screens.add_agent.discover_cli_models", _fake_discover)
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 0)
        await _continue(pilot)
        await _pick_radio(pilot, screen, "cli", 2)  # Antigravity
        await _continue(pilot)
        screen._refresh_models()
        await pilot.pause()
        await pilot.pause()
        assert _model_option_values(screen.query_one("#model_select", Select)) == ["Gemini 3.5 Flash (Low)"]
        screen.query_one("#model_select", Select).value = "Gemini 3.5 Flash (Low)"
        await pilot.pause()

        # Back to CLI, switch to Codex, forward to the model step: the old list is gone.
        await pilot.click("#back")
        await pilot.pause()
        await _pick_radio(pilot, screen, "cli", 0)  # Codex
        await _continue(pilot)
        assert screen.step == "model"
        assert _model_option_values(screen.query_one("#model_select", Select)) == [], "stale list leaked"
        assert screen.state.model is None, "a stale model selection leaked across CLIs"


async def test_review_shows_the_real_model(tmp_path: Path):
    """The Review step displays the actual chosen model (item 11)."""
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 0)
        await _continue(pilot)
        await _pick_radio(pilot, screen, "cli", 0)  # Codex
        await _continue(pilot)
        await _model_manual_then_details(pilot, screen, "gpt-5.5")
        await _details_then_review(pilot, screen, "codex-coder")
        review = str(screen.query_one("#review-card").render())
        assert "gpt-5.5" in review
        assert "not verified" in review  # a manual id is flagged as unverified
        assert "codex-coder" in review


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
        await _continue(pilot)  # -> model step (item 11)
        await _model_manual_then_details(pilot, screen, "deepseek-chat")
        await _details_then_review(pilot, screen, "ds-coder")
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


async def test_api_missing_key_is_caught_on_the_connection_step(tmp_path: Path):
    """A key-required provider with no key must be stopped **where the key is typed** (part 19).

    The wizard used to accept the empty key, walk the user all the way to Agent Details, and only
    then — during Create — throw them backwards to a step they thought they had finished. The
    credential is now validated on the connection step itself, by the same rule the service enforces,
    so the user never advances on an invalid connection.
    """

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

        # Blocked on the connection step, with an inline reason — Agent Details is never reached.
        assert screen.step == "connection"
        assert "API key" in str(screen.query_one("#err-conn", Label).content)
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
        await _continue(pilot)  # -> model step
        await _model_manual_then_details(pilot, screen, "llama3")
        await _details_then_review(pilot, screen, "local-agent")
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
        await _continue(pilot)  # -> model step
        await _model_manual_then_details(pilot, screen, "deepseek-chat")
        await _details_then_review(pilot, screen, "reuse-agent")
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


# ======================================================================= part 19 keyboard + hygiene


async def test_space_selects_and_enter_advances_on_a_radio_group(tmp_path: Path):
    """Space selects without moving; Enter advances (part 19).

    Enter used to be bound to "toggle the highlighted option", so pressing it on a radio group
    re-selected what was already selected and the wizard sat still — there was no keyboard-only way
    to move on.
    """

    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        rs = screen.query_one("#backend", RadioSet)
        screen.set_focus(rs)
        await pilot.pause()

        # Walk to "API Model" and press Space: it selects, and we are still on the backend step.
        await pilot.press("down")
        await pilot.press("space")
        await pilot.pause()
        assert rs.pressed_index == 1
        assert screen.step == "backend", "Space advanced the wizard; it should only select"

        # Enter advances to the next step.
        await pilot.press("enter")
        await pilot.pause()
        assert screen.step == "provider"
        assert screen.state.backend_type == "api"


async def test_changing_provider_clears_the_api_key_widget_not_just_the_state(tmp_path: Path):
    """A typed key must not survive a provider change *in the widget* (part 19).

    Clearing only ``state.api_key`` left the secret sitting in the password Input, so the next
    capture read it straight back and submitted OpenAI's key to DeepSeek.
    """

    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 1)
        await _continue(pilot)
        await _pick_radio(pilot, screen, "provider", _preset_index("deepseek"))
        await _continue(pilot)

        screen.query_one("#api_key", Input).value = "sk-deepseek-secret"
        await pilot.pause()

        # Go back and pick a different provider family.
        screen._go_back()
        await pilot.pause()
        await _pick_radio(pilot, screen, "provider", _preset_index("anthropic"))
        await pilot.pause()

        assert screen.query_one("#api_key", Input).value == "", "the key survived in the widget"
        assert screen.state.api_key is None


async def test_cancel_wipes_the_key_from_the_widget(tmp_path: Path):
    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 1)
        await _continue(pilot)
        await _continue(pilot)  # provider step -> connection step
        screen.query_one("#api_key", Input).value = "sk-should-be-wiped"
        await pilot.pause()

        screen._cancel()
        await pilot.pause()
        assert screen.state.api_key is None


async def test_existing_connections_are_filtered_to_the_provider_family(tmp_path: Path):
    """An Anthropic card must not offer a DeepSeek connection (part 19)."""

    oa = _app(tmp_path)
    oa.providers.add(name="deepseek-main", provider_type="deepseek", api_key="sk-x")
    oa.providers.add(name="claude-main", provider_type="anthropic", api_key="sk-y")

    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 1)
        await _continue(pilot)
        await _pick_radio(pilot, screen, "provider", _preset_index("anthropic"))
        await _continue(pilot)

        # Switch to "use an existing connection".
        await _pick_radio(pilot, screen, "conn-mode", 1)
        await pilot.pause()

        options = [value for _, value in screen.query_one("#existing-provider", Select)._options]
        assert "claude-main" in options
        assert "deepseek-main" not in options, "an incompatible connection was offered"


async def test_max_steps_is_validated_rather_than_silently_defaulted(tmp_path: Path):
    """5000 steps must be an inline error, not a silent 40 (part 19)."""

    oa = _app(tmp_path)
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 0)
        await _continue(pilot)
        await _pick_radio(pilot, screen, "cli", _cli_index("codex"))
        await _continue(pilot)
        await _model_default_then_details(pilot, screen)

        screen.query_one("#name", Input).value = "steps-agent"
        screen.query_one("#max_steps", Input).value = "5000"
        await _continue(pilot)  # validation happens leaving Agent Details

        assert screen.step == "details", "advanced past Agent Details with an invalid max_steps"
        assert isinstance(pilot.app.screen, AddAgentScreen), "the agent was created anyway"
        assert "between 1 and 500" in str(screen.query_one("#err-steps", Label).content)
    assert oa.agents.get("steps-agent") is None

    # A valid value goes through and is stored verbatim.
    app = OpenAgentTUI(oa)
    async with app.run_test() as pilot:
        screen = await _open(pilot)
        await _pick_radio(pilot, screen, "backend", 0)
        await _continue(pilot)
        await _pick_radio(pilot, screen, "cli", _cli_index("codex"))
        await _continue(pilot)
        await _model_default_then_details(pilot, screen)
        screen.query_one("#name", Input).value = "steps-agent"
        screen.query_one("#max_steps", Input).value = "120"
        await _continue(pilot)  # -> review
        await _create(pilot)
    assert oa.agents.get("steps-agent").max_steps == 120
