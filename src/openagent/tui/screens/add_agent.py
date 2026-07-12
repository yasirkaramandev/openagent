"""Add-Agent wizard — a backend-first, multi-step flow (spec §31, parts 1-3, 16).

The first screen asks *what kind of backend* to add (CLI Agent vs API Model) — never the full form.
From there the path forks:

* **CLI**: Backend → choose an installed CLI (Codex / Claude Code / Antigravity, from the live CLI
  registry) → configure the agent. An uninstalled CLI blocks Continue with a clear reason.
* **API**: Backend → choose a provider preset → connection (new *or* an existing saved connection) →
  configure the agent.

All state lives in an :class:`AddAgentWizardState` (the API key as a ``SecretStr``); the final Create
reads only that state and calls the existing application services — no business rules are duplicated
here. A fixed action bar keeps Continue/Create visible; a step indicator shows progress; Back
preserves non-secret input; Cancel destroys the state (and the secret). Expected, recoverable errors
are shown inline (never a full-screen traceback), while genuine bugs still propagate (part 15).
"""

from __future__ import annotations

from pydantic import SecretStr
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    RadioButton,
    RadioSet,
    Select,
    Static,
    TextArea,
)

from ...core.models import Protocol, RuntimeType
from ...core.permissions import profile_names
from ...credentials.store import CredentialError
from ...providers.factory import PRESETS, get_preset, preset_names
from ...runtimes.cli.registry import CliRegistryEntry, cli_registry_entries
from ...services.agent_service import AgentError
from ...services.provider_service import ProviderValidationError
from ..select_utils import selected_string
from ..wizard_state import AddAgentWizardState

try:  # keyring errors are environment-dependent; treat them as expected/recoverable when present
    from keyring.errors import KeyringError as _KeyringError
except Exception:  # pragma: no cover - keyring optional
    _KeyringError = ()  # type: ignore[assignment,misc]

#: Expected, recoverable errors shown inline (never a full-screen traceback). Anything *not* listed
#: is a genuine bug and is allowed to propagate so tests still fail (part 15).
_EXPECTED_ERRORS: tuple[type[BaseException], ...] = (AgentError, CredentialError, ValueError)
if isinstance(_KeyringError, type):
    _EXPECTED_ERRORS = (*_EXPECTED_ERRORS, _KeyringError)

_PROTOCOLS = [
    ("(preset default)", "preset"),
    ("openai-chat", Protocol.OPENAI_CHAT.value),
    ("openai-responses", Protocol.OPENAI_RESPONSES.value),
    ("anthropic-messages", Protocol.ANTHROPIC_MESSAGES.value),
]

_FINAL_STEPS = ("cli_config", "api_config")


class AddAgentScreen(Screen):
    BINDINGS = [
        Binding("escape", "back_or_cancel", "Back"),
        Binding("ctrl+s", "create", "Create Agent"),
        Binding("f10", "create", "Create Agent"),
    ]

    DEFAULT_CSS = """
    AddAgentScreen #step-indicator { padding: 0 2; text-style: bold; color: $accent; height: 1; }
    AddAgentScreen #content { height: 1fr; padding: 0 2; }
    AddAgentScreen Label { margin: 1 0 0 0; text-style: bold; }
    AddAgentScreen .hint { color: $text-muted; text-style: none; margin: 0 0 1 0; height: auto; }
    AddAgentScreen .field-error { color: $error; text-style: none; margin: 0; height: auto; }
    AddAgentScreen .card { border: round $primary; padding: 0 1; margin: 0 0 1 0; height: auto; }
    AddAgentScreen #error-summary { color: $error; padding: 0 2; height: auto; }
    AddAgentScreen #conn-status { height: auto; margin: 1 0 0 0; }
    AddAgentScreen #system_prompt { height: 5; border: round $primary; }
    AddAgentScreen RadioSet { height: auto; margin: 0 0 1 0; }
    AddAgentScreen #action-bar { height: 3; padding: 0 2; align-horizontal: left; background: $panel; }
    AddAgentScreen #action-bar Button { margin: 0 2 0 0; }
    AddAgentScreen #conn-actions { height: auto; margin: 1 0 0 0; }
    AddAgentScreen #conn-actions Button { margin: 0 2 0 0; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.state = AddAgentWizardState()
        self.step = "backend"
        self._cli_entries: list[CliRegistryEntry] = []
        self._cli_values: list[str] = []
        self._backend_values = ["cli", "api"]
        self._cred_values = ["keychain", "env", "none"]
        self._preset_values = preset_names()
        self._provider_names: list[str] = []

    # ------------------------------------------------------------------ compose

    def compose(self) -> ComposeResult:
        oa = self.app.oa  # type: ignore[attr-defined]
        self._provider_names = [p.name for p in oa.providers.list()]
        profiles = [(p, p) for p in profile_names()]

        yield Header()
        yield Static("Add Agent", classes="screen-title")
        yield Static("", id="step-indicator")
        with VerticalScroll(id="content"):
            # ---- Step 1: backend category ---------------------------------
            with Vertical(id="step-backend"):
                yield Static("What do you want to add?", classes="hint")
                with RadioSet(id="backend"):
                    yield RadioButton(
                        "CLI Agent — use an installed coding CLI (Codex, Claude Code, Antigravity)",
                        value=True, id="backend-cli",
                    )
                    yield RadioButton(
                        "API Model — connect OpenAI, Anthropic, DeepSeek, Qwen, Ollama, or another "
                        "compatible API",
                        id="backend-api",
                    )

            # ---- Step 2A: CLI selection -----------------------------------
            with Vertical(id="step-cli"):
                yield Static("Choose a CLI Agent", classes="hint")
                yield RadioSet(id="cli")
                yield Static("", id="cli-detail", classes="card")
                yield Label("", id="err-cli", classes="field-error")

            # ---- Step 2B: API provider selection --------------------------
            with Vertical(id="step-provider"):
                yield Static("Choose an API provider", classes="hint")
                with RadioSet(id="provider"):
                    for name in self._preset_values:
                        yield RadioButton(_preset_label(name), value=(name == "openai"),
                                          id=f"preset-{name}")
                yield Static("", id="provider-detail", classes="card")

            # ---- Step 3B: connection --------------------------------------
            with Vertical(id="step-connection"):
                yield Static("How should this API be connected?", classes="hint")
                with RadioSet(id="conn-mode"):
                    yield RadioButton("Create a new API connection", value=True, id="conn-new-mode")
                    if self._provider_names:
                        yield RadioButton("Use an existing connection", id="conn-existing-mode")

                with Vertical(id="conn-existing"):
                    yield Label("Provider connection")
                    yield Select([(n, n) for n in self._provider_names], id="existing-provider",
                                 allow_blank=True, prompt="select a saved connection")
                    yield Label("", id="err-provider", classes="field-error")

                with Vertical(id="conn-new"):
                    yield Label("Connection name")
                    yield Input(placeholder="e.g. deepseek-main", id="conn_name")
                    yield Label("", id="err-conn", classes="field-error")
                    yield Label("Protocol")
                    yield Select(_PROTOCOLS, value="preset", id="protocol", allow_blank=False)
                    yield Label("Base URL override (optional)")
                    yield Input(placeholder="leave blank to use the preset default", id="base_url")
                    yield Label("Region (when applicable)")
                    yield Input(placeholder="optional", id="region")
                    yield Label("Workspace ID (when applicable)")
                    yield Input(placeholder="optional", id="workspace_id")
                    yield Label("Credential source")
                    with RadioSet(id="cred"):
                        yield RadioButton("OS keychain", value=True, id="cred-keychain")
                        yield RadioButton("Environment variable", id="cred-env")
                        yield RadioButton("No API key (local provider)", id="cred-none")
                    with Vertical(id="key-row"):
                        yield Label("API key (hidden; stored in the OS keychain)")
                        yield Input(placeholder="paste key", password=True, id="api_key")
                    with Vertical(id="env-row"):
                        yield Label("Environment variable name")
                        yield Input(placeholder="e.g. DEEPSEEK_API_KEY", id="key_env")
                    with Horizontal(id="conn-actions"):
                        yield Button("Test Connection", id="test")

                yield Label("Model")
                yield Select([], id="model_select", allow_blank=True,
                             prompt="load models, or type an id below")
                yield Input(placeholder="remote model id, e.g. deepseek-chat", id="model")
                yield Label("", id="err-model", classes="field-error")
                with Horizontal():
                    yield Button("Load Models", id="load-models")
                yield Static("", id="conn-status")

            # ---- Step 3A final info (CLI) ---------------------------------
            yield Static("", id="cli-runtime-info", classes="card")
            # ---- Step 4B final info (API) ---------------------------------
            yield Static("", id="api-summary", classes="card")

            # ---- shared common agent fields (both final steps) ------------
            with Vertical(id="common-fields"):
                yield Label("Agent name")
                yield Input(placeholder="e.g. deepseek-coder (required)", id="name")
                yield Label("", id="err-name", classes="field-error")
                yield Label("Title")
                yield Input(placeholder="DeepSeek Backend Coder", id="title")
                yield Label("Description")
                yield Input(placeholder="what this agent is for", id="description")
                yield Label("Tags (comma-separated)")
                yield Input(placeholder="coder, python", id="tags")
                yield Label("Permission profile")
                yield Select(profiles, value="safe-edit", id="profile", allow_blank=False)
                yield Label("Maximum steps")
                yield Input(value="40", id="max_steps")
                yield Label("System prompt (optional)")
                yield TextArea(id="system_prompt")
            yield Static("", id="error-summary")

        with Horizontal(id="action-bar"):
            yield Button("Back", id="back")
            yield Button("Continue", variant="primary", id="continue")
            yield Button("Create Agent", variant="success", id="create")
            yield Button("Cancel", id="cancel")
        yield Footer()

    async def on_mount(self) -> None:
        # Populate the CLI radio from the live registry (install state, version, auth, status).
        self._cli_entries = await cli_registry_entries()
        self._cli_values = [e.type for e in self._cli_entries]
        cli_set = self.query_one("#cli", RadioSet)
        await cli_set.remove_children()
        for entry in self._cli_entries:
            await cli_set.mount(RadioButton(_cli_button_label(entry), id=f"cli-{entry.type}"))
        # A RadioSet auto-selects a value=True button in its own _on_mount, which already ran on the
        # (then empty) placeholder; pressing the first button now registers it as the pressed one.
        buttons = list(cli_set.query(RadioButton))
        if buttons:
            buttons[0].value = True
            cli_set._selected = 0  # deterministic keyboard-nav anchor (matches compose-time sets)
        self.state.set_backend("cli")
        self._show_step("backend")

    # ------------------------------------------------------------------ step machine

    def _step_list(self) -> list[str]:
        backend = self.state.backend_type or "cli"
        if backend == "api":
            return ["backend", "provider", "connection", "api_config"]
        return ["backend", "cli", "cli_config"]

    def _show_step(self, step: str) -> None:
        self.step = step
        for sid in ("step-backend", "step-cli", "step-provider", "step-connection"):
            self.query_one(f"#{sid}").display = False
        self.query_one("#cli-runtime-info").display = False
        self.query_one("#api-summary").display = False
        self.query_one("#common-fields").display = False

        if step == "backend":
            self.query_one("#step-backend").display = True
        elif step == "cli":
            self.query_one("#step-cli").display = True
            self._sync_cli_detail()
        elif step == "provider":
            self.query_one("#step-provider").display = True
            self._sync_provider_detail()
        elif step == "connection":
            self.query_one("#step-connection").display = True
            self._sync_connection_fields()
        elif step == "cli_config":
            self.query_one("#cli-runtime-info").display = True
            self.query_one("#common-fields").display = True
            self._render_cli_runtime_info()
        elif step == "api_config":
            self.query_one("#api-summary").display = True
            self.query_one("#common-fields").display = True
            self._render_api_summary()

        self._update_indicator()
        self._update_action_bar()
        self._focus_step(step)

    def _update_indicator(self) -> None:
        steps = self._step_list()
        titles = {
            "backend": "Backend", "cli": "CLI", "provider": "Provider",
            "connection": "Connection", "cli_config": "Agent details",
            "api_config": "Agent details",
        }
        idx = steps.index(self.step) + 1
        self.query_one("#step-indicator", Static).update(
            f"Step {idx} of {len(steps)} — {titles[self.step]}"
        )

    def _update_action_bar(self) -> None:
        is_final = self.step in _FINAL_STEPS
        self.query_one("#back").display = self.step != "backend"
        self.query_one("#continue").display = not is_final
        self.query_one("#create").display = is_final

    def _focus_step(self, step: str) -> None:
        focus_map = {
            "backend": "#backend", "cli": "#cli", "provider": "#provider",
            "connection": "#conn-mode", "cli_config": "#name", "api_config": "#name",
        }
        try:
            self.query_one(focus_map[step]).focus()
        except Exception:  # pragma: no cover - focus is best-effort
            pass

    # ------------------------------------------------------------------ navigation

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "continue":
            self.action_continue()
        elif bid == "create":
            self.action_create()
        elif bid == "back":
            self._go_back()
        elif bid == "cancel":
            self._cancel()
        elif bid == "test":
            self._test_connection()
        elif bid == "load-models":
            self._load_models()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in a field advances (Continue) on non-final steps (part 16).
        if self.step not in _FINAL_STEPS:
            self.action_continue()

    def action_back_or_cancel(self) -> None:
        if self.step == "backend":
            self._cancel()
        else:
            self._go_back()

    def action_continue(self) -> None:
        self._clear_errors()
        try:
            if self._commit_step():
                self._advance()
        except _EXPECTED_ERRORS as exc:
            self._summary([str(exc)])

    def _advance(self) -> None:
        steps = self._step_list()
        i = steps.index(self.step)
        if i + 1 < len(steps):
            self._show_step(steps[i + 1])

    def _go_back(self) -> None:
        # Persist whatever the current step holds (non-secret) before stepping back.
        try:
            self._capture_step(self.step)
        except _EXPECTED_ERRORS:
            pass
        steps = self._step_list()
        i = steps.index(self.step)
        if i > 0:
            self._show_step(steps[i - 1])

    def _cancel(self) -> None:
        self.state.clear_secret()
        self.state = AddAgentWizardState()
        self.app.pop_screen()

    # ------------------------------------------------------------------ per-step commit/capture

    def _commit_step(self, *, validate: bool = True) -> bool:
        """Write the current step into state; return True if the step is valid to advance."""
        return self._capture_step(self.step, validate=validate)

    def _capture_step(self, step: str, *, validate: bool = True) -> bool:
        if step == "backend":
            self.state.set_backend("api" if self._radio_value("#backend", self._backend_values) == "api" else "cli")
            return True
        if step == "cli":
            cli = self._radio_value("#cli", self._cli_values)
            if validate and not cli:
                self._field_error("err-cli", "Choose a CLI")
                return False
            entry = self._entry_for(cli)
            if validate and entry is not None and not entry.installed:
                self._field_error(
                    "err-cli", "Install or configure this CLI before creating the agent"
                )
                return False
            self.state.set_cli(cli or "", entry.executable if entry else None)
            return True
        if step == "provider":
            preset = self._radio_value("#provider", self._preset_values) or "openai"
            self.state.set_provider_type(preset)
            return True
        if step == "connection":
            return self._capture_connection(validate=validate)
        if step in ("cli_config", "api_config"):
            return self._capture_common(validate=validate)
        return True

    def _capture_connection(self, *, validate: bool) -> bool:
        mode = self._radio_value("#conn-mode", ["new", "existing"]) or "new"
        self.state.set_provider_mode(mode)  # type: ignore[arg-type]
        model = self._input("model") or None
        if mode == "existing":
            provider = selected_string(self.query_one("#existing-provider", Select))
            if validate and not provider:
                self._field_error("err-provider", "required")
                return False
            self.state.set_existing_provider(provider or "")
            self.state.model = model
            if validate and not model:
                self._field_error("err-model", "choose or type a model id")
                return False
            return True
        # new connection
        conn_name = self._input("conn_name")
        if validate and not conn_name:
            self._field_error("err-conn", "connection name is required")
            return False
        self.state.provider_name = conn_name
        protocol_val = selected_string(self.query_one("#protocol", Select))
        self.state.protocol = None if protocol_val in (None, "preset") else Protocol(protocol_val)
        self.state.base_url = self._input("base_url") or None
        self.state.region = self._input("region") or None
        self.state.workspace_id = self._input("workspace_id") or None
        cred = self._radio_value("#cred", self._cred_values) or "keychain"
        self.state.credential_source = cred
        key = self.query_one("#api_key", Input).value if cred == "keychain" else ""
        self.state.api_key = SecretStr(key) if key else None
        self.state.key_env = self._input("key_env") or None if cred == "env" else None
        self.state.model = model
        if validate and not model:
            self._field_error("err-model", "choose or type a model id")
            return False
        return True

    def _capture_common(self, *, validate: bool) -> bool:
        name = self._input("name")
        self.state.agent_name = name
        self.state.title = self._input("title")
        self.state.description = self._input("description")
        self.state.tags = [t.strip() for t in self._input("tags").split(",") if t.strip()]
        self.state.system_prompt = self.query_one("#system_prompt", TextArea).text.strip()
        self.state.permission_profile = (
            selected_string(self.query_one("#profile", Select)) or "safe-edit"
        )
        self.state.max_steps = _parse_int(self._input("max_steps"), 40)
        if validate and not name:
            self._field_error("err-name", "required")
            return False
        return True

    # ------------------------------------------------------------------ create

    def action_create(self) -> None:
        if self.step not in _FINAL_STEPS:
            return
        self._clear_errors()
        try:
            if not self._capture_common(validate=True):
                return
            self._create()
        except _EXPECTED_ERRORS as exc:
            self._summary([str(exc)])

    def _create(self) -> None:
        oa = self.app.oa  # type: ignore[attr-defined]
        s = self.state
        common = {
            "title": s.title, "description": s.description, "tags": s.tags,
            "system_prompt": s.system_prompt, "permission_profile": s.permission_profile,
            "max_steps": s.max_steps,
        }
        if s.backend_type == "cli":
            agent = oa.agents.create(name=s.agent_name, runtime_type=RuntimeType.CLI,
                                     cli=s.cli_type, **common)
        elif s.provider_mode == "existing":
            agent = oa.agents.create(name=s.agent_name, runtime_type=RuntimeType.API_AGENT,
                                     provider=s.provider_name, model=s.model, **common)
        else:
            agent = self._create_with_new_connection(common)
            if agent is None:
                return
        s.clear_secret()
        self.notify(f"agent '{agent.name}' created — OPENAGENT.md updated", severity="information")
        self.state = AddAgentWizardState()
        self.app.pop_screen()
        self.app.open_section("agents")  # type: ignore[attr-defined]

    def _create_with_new_connection(self, common: dict):
        oa = self.app.oa  # type: ignore[attr-defined]
        s = self.state
        if oa.providers.get(s.provider_name):
            self._field_error("err-conn", "already exists")
            self._summary([f"a provider named {s.provider_name!r} already exists"])
            return None
        try:
            return oa.agents.create_with_new_provider(
                provider_name=s.provider_name, provider_type=s.provider_type, protocol=s.protocol,
                base_url=s.base_url, region=s.region, workspace_id=s.workspace_id,
                api_key=s.api_key.get_secret_value() if s.api_key else None,
                key_env=s.key_env, credential_source=s.credential_source,
                model=s.model, name=s.agent_name, **common,
            )
        except ProviderValidationError as exc:
            field = "err-conn" if exc.field == "name" else "err-name"
            if exc.field in ("api_key", "key_env"):
                # Surface a key/env error on the connection step, and step back to it.
                self._show_step("connection")
                self._field_error("err-model", "")
                self.query_one("#conn-status", Static).update(f"[red]✗ {exc}[/red]")
            self._field_error(field, "invalid")
            self._summary([str(exc)])
            return None

    # ------------------------------------------------------------------ live sync / details

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        rid = event.radio_set.id
        if rid == "backend":
            self.state.set_backend("api" if self._radio_value("#backend", self._backend_values) == "api" else "cli")
        elif rid == "cli":
            self._sync_cli_detail()
        elif rid == "provider":
            new = self._radio_value("#provider", self._preset_values)
            if new and new != self.state.provider_type:
                # A different provider invalidates the connection/credential/model — clear both the
                # state and the widgets so nothing stale from the previous provider is submitted.
                self.state.set_provider_type(new)
                self._reset_connection_widgets()
            self._sync_provider_detail()
        elif rid == "cred":
            self._sync_connection_fields()
        elif rid == "conn-mode":
            self._sync_connection_fields()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "model_select":
            chosen = selected_string(event.select)
            if chosen:
                self.query_one("#model", Input).value = chosen

    def _sync_cli_detail(self) -> None:
        entry = self._entry_for(self._radio_value("#cli", self._cli_values))
        self.query_one("#cli-detail", Static).update(_cli_detail_text(entry))

    def _sync_provider_detail(self) -> None:
        name = self._radio_value("#provider", self._preset_values)
        self.query_one("#provider-detail", Static).update(_provider_detail_text(name))

    def _reset_connection_widgets(self) -> None:
        """Clear the connection-step widgets (called when the provider preset changes)."""
        for wid in ("conn_name", "base_url", "region", "workspace_id", "api_key", "key_env", "model"):
            try:
                self.query_one(f"#{wid}", Input).value = ""
            except Exception:  # pragma: no cover - widget always present
                pass
        self.query_one("#model_select", Select).set_options([])
        self._status("")

    def _sync_connection_fields(self) -> None:
        mode = self._radio_value("#conn-mode", ["new", "existing"]) or "new"
        is_new = mode == "new"
        self.query_one("#conn-new").display = is_new
        self.query_one("#conn-existing").display = not is_new
        cred = self._radio_value("#cred", self._cred_values) or "keychain"
        self.query_one("#key-row").display = is_new and cred == "keychain"
        self.query_one("#env-row").display = is_new and cred == "env"

    def _render_cli_runtime_info(self) -> None:
        entry = self._entry_for(self.state.cli_type)
        self.query_one("#cli-runtime-info", Static).update(_cli_detail_text(entry))

    def _render_api_summary(self) -> None:
        s = self.state
        conn = s.provider_name or "(unnamed)"
        self.query_one("#api-summary", Static).update(
            f"[b]Provider connection:[/b] {conn}\n[b]Model:[/b] {s.model or '(none)'}\n"
            f"[b]Mode:[/b] {s.provider_mode or 'new'}"
        )

    # ------------------------------------------------------------------ workers (test / load models)

    def _new_conn_params(self) -> dict:
        # Read the live connection widgets for a transient test/discovery (never persisted).
        protocol_val = selected_string(self.query_one("#protocol", Select))
        protocol = None if protocol_val in (None, "preset") else Protocol(protocol_val)
        cred = self._radio_value("#cred", self._cred_values) or "keychain"
        return {
            "provider_type": self._radio_value("#provider", self._preset_values) or "custom",
            "protocol": protocol,
            "base_url": self._input("base_url") or None,
            "region": self._input("region") or None,
            "workspace_id": self._input("workspace_id") or None,
            "api_key": (self.query_one("#api_key", Input).value or None) if cred == "keychain" else None,
            "key_env": self._input("key_env") or None if cred == "env" else None,
        }

    def _test_connection(self) -> None:
        self._status("[dim]testing…[/dim]")
        self.run_worker(self._do_test(self._new_conn_params()), exclusive=True)

    async def _do_test(self, p: dict) -> None:
        oa = self.app.oa  # type: ignore[attr-defined]
        try:
            result = await oa.providers.test_config(
                provider_type=p["provider_type"], protocol=p["protocol"], base_url=p["base_url"],
                region=p["region"], workspace_id=p["workspace_id"],
                api_key=p["api_key"], key_env=p["key_env"],
            )
        except Exception as exc:  # noqa: BLE001 - surface any failure as an unhealthy result
            self._status(f"[red]✗ {exc}[/red]")
            return
        self._status(f"[green]✓ connection ok[/green] — {result.detail}" if result.ok
                     else f"[red]✗ {result.detail}[/red]")

    def _load_models(self) -> None:
        self._status("[dim]loading models…[/dim]")
        mode = self._radio_value("#conn-mode", ["new", "existing"]) or "new"
        if mode == "existing":
            provider = selected_string(self.query_one("#existing-provider", Select))
            if not provider:
                self._status("[red]select a provider connection first[/red]")
                return
            self.run_worker(self._load_models_existing(provider), exclusive=True)
        else:
            self.run_worker(self._load_models_new(self._new_conn_params()), exclusive=True)

    async def _load_models_existing(self, provider: str) -> None:
        oa = self.app.oa  # type: ignore[attr-defined]
        try:
            models = await oa.providers.remote_models(provider)
        except Exception as exc:  # noqa: BLE001 - discovery is best-effort
            self._status(f"[red]could not load models: {exc}[/red]")
            return
        self._populate_models(models)

    async def _load_models_new(self, p: dict) -> None:
        oa = self.app.oa  # type: ignore[attr-defined]
        try:
            models = await oa.providers.remote_models_config(
                provider_type=p["provider_type"], protocol=p["protocol"], base_url=p["base_url"],
                region=p["region"], workspace_id=p["workspace_id"],
                api_key=p["api_key"], key_env=p["key_env"],
            )
        except Exception as exc:  # noqa: BLE001 - discovery is best-effort
            self._status(f"[red]could not load models: {exc}[/red]")
            return
        self._populate_models(models)

    def _populate_models(self, models) -> None:
        ids = [m.id for m in models]
        select = self.query_one("#model_select", Select)
        if not ids:
            self._status("[yellow]no models reported — type a model id manually below[/yellow]")
            select.set_options([])
            return
        select.set_options([(mid, mid) for mid in ids])
        self._status(f"[green]loaded {len(ids)} model(s)[/green] — pick one, or type an id below")

    # ------------------------------------------------------------------ helpers

    def _radio_value(self, selector: str, values: list[str]) -> str | None:
        radio = self.query_one(selector, RadioSet)
        idx = radio.pressed_index
        if 0 <= idx < len(values):
            return values[idx]
        return None

    def _entry_for(self, cli_type: str | None) -> CliRegistryEntry | None:
        return next((e for e in self._cli_entries if e.type == cli_type), None)

    def _input(self, wid: str) -> str:
        return self.query_one(f"#{wid}", Input).value.strip()

    def _status(self, message: str) -> None:
        self.query_one("#conn-status", Static).update(message)

    def _field_error(self, wid: str, message: str) -> None:
        self.query_one(f"#{wid}", Label).update(f"✗ {message}" if message else "")

    def _clear_errors(self) -> None:
        for wid in ("err-name", "err-cli", "err-provider", "err-conn", "err-model"):
            self.query_one(f"#{wid}", Label).update("")
        self.query_one("#error-summary", Static).update("")

    def _summary(self, errors: list[str]) -> None:
        # Plain/escaped text: user-provided content must not become Rich markup (part 16).
        from rich.markup import escape
        self.query_one("#error-summary", Static).update(
            "[b]Cannot continue:[/b]\n" + "\n".join(f"  • {escape(e)}" for e in errors)
        )


# --------------------------------------------------------------------------- label/detail rendering


def _preset_label(name: str) -> str:
    p = PRESETS[name]
    where = "local" if not p.needs_key else "cloud"
    key = "no key" if not p.needs_key else "key required"
    return f"{p.label}  ·  {p.protocol.value}  ·  {where}  ·  {key}"


def _cli_button_label(entry: CliRegistryEntry) -> str:
    state = "installed" if entry.installed else "not installed"
    return f"{entry.display_name}  ·  {state}"


def _cli_detail_text(entry: CliRegistryEntry | None) -> str:
    if entry is None:
        return "(select a CLI)"
    lines = [
        f"[b]{entry.display_name}[/b]  (registry key: {entry.type})",
        f"Installed: {'yes' if entry.installed else 'no'}",
    ]
    if entry.installed:
        lines.append(f"Executable: {entry.executable or '(unknown)'}")
        if entry.version:
            lines.append(f"Version: {entry.version}")
        auth = {True: "authenticated", False: "not detected", None: "unknown"}[entry.authenticated]
        lines.append(f"Authentication: {auth}")
        lines.append(f"Adapter: {entry.adapter}")
    else:
        lines.append("Install or configure the CLI before creating this agent.")
    lines.append(f"Status: {entry.status_label}")
    return "\n".join(lines)


def _provider_detail_text(name: str | None) -> str:
    if not name:
        return "(select a provider)"
    p = get_preset(name)
    if p is None:
        return name
    where = "local service" if not p.needs_key else "cloud"
    key = "no API key" if not p.needs_key else "API key required"
    return (
        f"[b]{p.label}[/b]\nProtocol: {p.protocol.value}\n{where} · {key}\n"
        "Model discovery: supported (best-effort) · Compatibility: adapter implemented, live-unverified"
    )


def _parse_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
