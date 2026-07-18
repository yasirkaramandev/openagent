"""Add-Agent wizard — a backend-first, multi-step flow (spec §31, parts 1-3, 16; item 11).

The first screen asks *what kind of backend* to add (CLI Agent vs API Model) — never the full form.
From there the path forks, and **model selection is its own step** (item 11), never buried in Agent
Details:

* **CLI**:  Backend → CLI → Model → Agent Details → Review → Create
* **API**:  Backend → Provider → Connection → Model → Agent Details → Review → Create

All state lives in an :class:`AddAgentWizardState` (the API key as a ``SecretStr``); the final Create
reads only that state and calls the existing application services — no business rules are duplicated
here. A fixed action bar keeps Continue/Create visible; a step indicator shows progress; Back
preserves non-secret input; Cancel destroys the state (and the secret). Expected, recoverable errors
are shown inline (never a full-screen traceback), while genuine bugs still propagate (part 15).
"""

from __future__ import annotations

import os
import webbrowser

from pydantic import SecretStr
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
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

from ...core.models import Protocol, RemoteModel, RuntimeType
from ...core.permissions import profile_names
from ...credentials.store import CredentialError
from ...providers.discovery import (
    AgentModelProbe,
    ModelDiscoveryResult,
    filter_models,
    looks_non_chat,
    publishers,
)
from ...providers.factory import PRESETS, ProviderPreset, get_preset, preset_names
from ...runtimes.cli.registry import (
    CliModelDiscovery,
    CliRegistryEntry,
    cli_registry_entries,
    discover_cli_models,
)
from ...services.agent_service import AgentError
from ...services.provider_service import ProviderValidationError, resolve_credential
from ..markup import safe_markup
from ..secret_input import SecretInputMixin
from ..select_utils import selected_string
from ..wizard_state import AddAgentWizardState, BackendType

#: A single agent-loop bound. 40 is the default; anything outside this is a mistake, not a preference.
MIN_STEPS, MAX_STEPS = 1, 500

#: Model-selection modes on the dedicated Model step (item 11).
_MODEL_MODES = ["discovered", "manual", "default"]


class WizardRadioSet(RadioSet):
    """A RadioSet where **Space selects** and **Enter advances** the wizard (part 19).

    Textual binds both Space *and* Enter to "toggle the highlighted option", so Enter on a radio
    group did nothing except re-select what was already selected — the user pressed Enter, the wizard
    sat still, and there was no way to move on without reaching for the mouse or Tab. Enter is
    intercepted here (before the binding runs) and turned into "continue".
    """

    class Advance(Message):
        """Enter was pressed on a radio group: move to the next wizard step."""

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()  # keep RadioSet's own enter->toggle binding from firing
            event.stop()
            self.post_message(self.Advance())


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

#: The Create button lives only on the Review step (item 11).
_FINAL_STEPS = ("review",)


class AddAgentScreen(SecretInputMixin, Screen):
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
    AddAgentScreen .warning { color: $warning; text-style: none; margin: 0 0 1 0; height: auto; }
    AddAgentScreen .field-error { color: $error; text-style: none; margin: 0; height: auto; }
    AddAgentScreen #hosted-actions { height: auto; margin: 0 0 1 0; }
    AddAgentScreen #catalog-filters { height: auto; }
    AddAgentScreen .card { border: round $primary; padding: 0 1; margin: 0 0 1 0; height: auto; }
    AddAgentScreen #error-summary { color: $error; padding: 0 2; height: auto; }
    AddAgentScreen #conn-status { height: auto; margin: 1 0 0 0; }
    AddAgentScreen #system_prompt { height: 5; border: round $primary; }
    AddAgentScreen RadioSet { height: auto; margin: 0 0 1 0; }
    AddAgentScreen #action-bar { height: 3; padding: 0 2; align-horizontal: left; background: $panel; }
    AddAgentScreen #action-bar Button { margin: 0 2 0 0; }
    AddAgentScreen #conn-actions { height: auto; margin: 1 0 0 0; }
    AddAgentScreen #conn-actions Button { margin: 0 2 0 0; }
    AddAgentScreen #model-actions { height: auto; margin: 1 0 0 0; }
    AddAgentScreen #review-card { border: round $accent; padding: 0 1; margin: 0 0 1 0; height: auto; }
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
        #: What the currently-shown model list was discovered for — so switching CLI/provider resets
        #: it and a stale model never leaks across backends (item 11).
        self._model_context: tuple[str, ...] | None = None
        self._model_method = ""  # e.g. "agy models" / "provider models"
        self._model_status = "default"  # discovered | manual | default (shown on Review)
        #: The full discovered catalog, kept so search/publisher filtering is purely local (§14.2).
        self._catalog: list[RemoteModel] = []
        self._catalog_error: tuple[str, str] | None = None
        #: The last real capability probe — the ONLY thing that may call a model agent-compatible.
        self._probe: AgentModelProbe | None = None

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
                with WizardRadioSet(id="backend"):
                    yield RadioButton(
                        "CLI Agent — use an installed coding CLI (Codex, Claude Code, Antigravity)",
                        value=True,
                        id="backend-cli",
                    )
                    yield RadioButton(
                        "API Model — connect OpenAI, Anthropic, DeepSeek, Qwen, Ollama, or another "
                        "compatible API",
                        id="backend-api",
                    )

            # ---- Step 2A: CLI selection -----------------------------------
            with Vertical(id="step-cli"):
                yield Static("Choose a CLI Agent", classes="hint")
                yield WizardRadioSet(id="cli")
                yield Static("", id="cli-detail", classes="card")
                yield Label("", id="err-cli", classes="field-error")

            # ---- Step 2B: API provider selection --------------------------
            with Vertical(id="step-provider"):
                yield Static("Choose an API provider", classes="hint")
                with WizardRadioSet(id="provider"):
                    for name in self._preset_values:
                        yield RadioButton(
                            _preset_label(name), value=(name == "openai"), id=f"preset-{name}"
                        )
                yield Static("", id="provider-detail", classes="card")

            # ---- Step 3B: connection --------------------------------------
            with Vertical(id="step-connection"):
                yield Static("How should this API be connected?", classes="hint")
                with WizardRadioSet(id="conn-mode"):
                    yield RadioButton("Create a new API connection", value=True, id="conn-new-mode")
                    if self._provider_names:
                        yield RadioButton("Use an existing connection", id="conn-existing-mode")

                with Vertical(id="conn-existing"):
                    yield Label("Provider connection")
                    # Populated in _sync_connection_fields, filtered to the selected provider family:
                    # offering an Anthropic card a `deepseek-main` connection is a guaranteed failure.
                    yield Select(
                        [],
                        id="existing-provider",
                        allow_blank=True,
                        prompt="select a saved connection",
                    )
                    yield Label("", id="err-provider", classes="field-error")

                with Vertical(id="conn-new"):
                    # Provider-aware header: for a hosted catalog (NVIDIA Build) the endpoint and
                    # protocol are fixed facts from the official docs, not user input (§13.1).
                    yield Static("", id="hosted-info", classes="card")
                    with Horizontal(id="hosted-actions"):
                        yield Button("Open NVIDIA Build", id="open-catalog")
                    yield Static("", id="hosted-hint", classes="hint")
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
                    with WizardRadioSet(id="cred"):
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
                yield Static("", id="conn-status")

            # ---- Model step (shared by CLI + API) -------------------------
            with Vertical(id="step-model"):
                yield Static("Choose the model", classes="hint")
                yield Static("", id="model-info", classes="card")
                # A mixed catalog (NVIDIA Build) lists chat, embedding, rerank and vision models
                # alike — a listing is never a capability claim (§14.3).
                yield Static("", id="catalog-warning", classes="warning")
                with WizardRadioSet(id="model-mode"):
                    yield RadioButton(
                        "Choose a discovered model", value=True, id="model-mode-discovered"
                    )
                    yield RadioButton(
                        "Enter a model ID manually (not verified)", id="model-mode-manual"
                    )
                    yield RadioButton(
                        "Use the CLI's default model (CLI agents only)", id="model-mode-default"
                    )
                # Local search + publisher filter: a big catalog is unusable as a flat Select, and
                # typing must never trigger a network call (§14.2).
                with Vertical(id="catalog-filters"):
                    yield Label("Search models")
                    yield Input(placeholder="filter by model id (local)", id="model-search")
                    yield Label("Publisher")
                    yield Select([], id="model-owner", allow_blank=True, prompt="all publishers")
                yield Select(
                    [],
                    id="model_select",
                    allow_blank=True,
                    prompt="discovered models (Refresh to load)",
                )
                yield Input(placeholder="model id/label, e.g. publisher/model", id="model")
                yield Static("", id="model-status", classes="hint")
                yield Static("", id="model-verify", classes="hint")
                with Horizontal(id="model-actions"):
                    yield Button("Refresh Catalog", id="model-refresh")
                    yield Button("Validate Model & Key", id="model-validate")
                    yield Button("Open model page", id="open-model-page")
                yield Checkbox(
                    "Create as unverified/limited model (advanced)",
                    value=False,
                    id="allow-unverified",
                )
                with Vertical(id="override-row"):
                    yield Label("Required override reason")
                    yield Input(
                        placeholder="why is running this unverified model acceptable?",
                        id="override-reason",
                    )
                yield Label("", id="err-model", classes="field-error")

            # ---- Agent Details step (common fields) -----------------------
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
                yield Label("", id="err-steps", classes="field-error")
                yield Label("System prompt (optional)")
                yield TextArea(id="system_prompt")

            # ---- Review step ----------------------------------------------
            with Vertical(id="step-review"):
                yield Static("Review — press Create to add this agent", classes="hint")
                yield Static("", id="review-card", classes="review-card")

            yield Static("", id="error-summary")

        with Horizontal(id="action-bar", classes="action-bar"):
            yield Button("Back", id="back")
            yield Button("Continue", variant="primary", id="continue")
            yield Button("Create Agent", variant="success", id="create")
            yield Button("Cancel", id="cancel")
        yield Footer()

    async def on_mount(self) -> None:
        # Populate the CLI radio from the live registry (install state, version, auth, status).
        self._cli_entries = await cli_registry_entries()
        self._cli_values = [e.type for e in self._cli_entries]
        cli_set = self.query_one("#cli", WizardRadioSet)
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
            return ["backend", "provider", "connection", "model", "details", "review"]
        return ["backend", "cli", "model", "details", "review"]

    def _show_step(self, step: str) -> None:
        self.step = step
        for sid in (
            "step-backend",
            "step-cli",
            "step-provider",
            "step-connection",
            "step-model",
            "common-fields",
            "step-review",
        ):
            self.query_one(f"#{sid}").display = False

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
        elif step == "model":
            self.query_one("#step-model").display = True
            self._enter_model_step()
        elif step == "details":
            self.query_one("#common-fields").display = True
        elif step == "review":
            self.query_one("#step-review").display = True
            self._render_review()

        self._update_indicator()
        self._update_action_bar()
        self._focus_step(step)

    def _update_indicator(self) -> None:
        steps = self._step_list()
        titles = {
            "backend": "Backend",
            "cli": "CLI",
            "provider": "Provider",
            "connection": "Connection",
            "model": "Model",
            "details": "Agent details",
            "review": "Review",
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
            "backend": "#backend",
            "cli": "#cli",
            "provider": "#provider",
            "connection": "#conn-mode",
            "model": "#model-mode",
            "details": "#name",
            "review": "#create",
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
        elif bid == "model-refresh":
            self._refresh_models()
        elif bid == "model-validate":
            self._validate_model()
        elif bid == "open-catalog":
            preset = self._preset()
            self._open_url((preset.catalog_url if preset else None) or "https://build.nvidia.com/")
        elif bid == "open-model-page":
            preset = self._preset()
            self._open_url((preset.catalog_url if preset else None) or "https://build.nvidia.com/")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in a field advances (Continue) on non-final steps (part 16).
        if self.step not in _FINAL_STEPS:
            self.action_continue()

    def on_wizard_radio_set_advance(self, event: WizardRadioSet.Advance) -> None:
        """Enter on a radio group continues; Space (Textual's own binding) just selects (part 19)."""

        event.stop()
        if self.step in _FINAL_STEPS:
            self.action_create()
        else:
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
            self._clear_secret_widget()
            self._summary([str(exc)])

    def _advance(self) -> None:
        steps = self._step_list()
        i = steps.index(self.step)
        if i + 1 < len(steps):
            self._show_step(steps[i + 1])

    def _go_back(self) -> None:
        # Persist whatever the current step holds (non-secret) before stepping back.
        try:
            self._capture_step(self.step, validate=False)
        except _EXPECTED_ERRORS:
            pass
        steps = self._step_list()
        i = steps.index(self.step)
        if i > 0:
            self._show_step(steps[i - 1])

    def _cancel(self) -> None:
        self._clear_secret_widget()
        self.state = AddAgentWizardState()
        self.app.pop_screen()

    def on_unmount(self) -> None:
        # Leaving the screen by any route must not leave a key in a widget or in memory (part 19).
        self._clear_secret_widget()

    # ------------------------------------------------------------------ per-step commit/capture

    def _commit_step(self, *, validate: bool = True) -> bool:
        """Write the current step into state; return True if the step is valid to advance."""
        return self._capture_step(self.step, validate=validate)

    def _capture_step(self, step: str, *, validate: bool = True) -> bool:
        if step == "backend":
            self.state.set_backend(
                "api" if self._radio_value("#backend", self._backend_values) == "api" else "cli"
            )
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
        if step == "model":
            return self._capture_model(validate=validate)
        if step == "details":
            return self._capture_common(validate=validate)
        return True  # review: nothing to capture

    def _capture_connection(self, *, validate: bool) -> bool:
        mode = self._radio_value("#conn-mode", ["new", "existing"]) or "new"
        self.state.set_provider_mode(mode)  # type: ignore[arg-type]
        if mode == "existing":
            provider = selected_string(self.query_one("#existing-provider", Select))
            if validate and not provider:
                self._field_error("err-provider", "choose a saved connection")
                return False
            self.state.set_existing_provider(provider or "")
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

        if validate and not self._validate_credential(conn_name, cred, key):
            return False
        return True

    def _validate_credential(self, conn_name: str, cred: str, key: str) -> bool:
        """Check the credential **on the connection step**, where the user can fix it (part 19).

        The wizard used to accept any credential here and only discover the problem during Create —
        on the *Agent Details* step — at which point it threw the user backwards to a screen they
        thought they had finished. The rule is the same one the service enforces
        (:func:`resolve_credential`), so there is one source of truth and no drift.
        """

        env_var = self._input("key_env") or None
        try:
            resolve_credential(
                name=conn_name,
                provider_type=self.state.provider_type or "custom",
                api_key=key or None,
                key_env=env_var,
                credential_source=cred,
            )
        except ProviderValidationError as exc:
            field = {"api_key": "err-conn", "key_env": "err-conn"}.get(exc.field, "err-conn")
            self._field_error(field, str(exc))
            self._status(f"[red]✗ {safe_markup(str(exc), 200)}[/red]")
            return False
        # An env-var credential that does not exist is a guaranteed failure at run time — say so
        # here, where the user can fix it, and never echo the value (§13.3).
        if cred == "env" and env_var and env_var not in os.environ:
            message = (
                f"environment variable {env_var} is not set in this environment — export it "
                "first, or use the OS keychain instead"
            )
            self._field_error("err-conn", message)
            self._status(f"[red]✗ {safe_markup(message, 200)}[/red]")
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

        steps, steps_error = _parse_steps(self._input("max_steps"))
        if validate and steps_error:
            # An out-of-range or non-numeric value used to be silently replaced with 40 — the user
            # asked for 5000 steps and got 40 without a word (part 19).
            self._field_error("err-steps", steps_error)
            return False
        self.state.max_steps = steps

        if validate and not name:
            self._field_error("err-name", "required")
            return False
        return True

    # ------------------------------------------------------------------ model step (item 11)

    def _model_backend_key(self) -> tuple[str, ...]:
        """Identity of the backend the model list belongs to — changes reset the list."""
        s = self.state
        if s.backend_type == "cli":
            return ("cli", s.cli_type or "")
        return ("api", s.provider_mode or "new", s.provider_name or "", s.provider_type or "")

    def _enter_model_step(self) -> None:
        key = self._model_backend_key()
        if key != self._model_context:
            # A different CLI/provider: drop any stale discovered list and selection (item 11).
            self._model_context = key
            self.query_one("#model_select", Select).set_options([])
            self.query_one("#model", Input).value = ""
            self.state.model = None
            self._model_method = ""
            self._catalog = []
            self._catalog_error = None
            self._set_model_status(
                "[dim]Refresh to discover models, or enter an id / use the CLI default[/dim]"
            )
        self.query_one("#model-info", Static).update(self._model_info_text())
        self._sync_model_fields()
        # Discovery is explicit (Refresh) — never an automatic network call on entry, and never a
        # blocking one when it runs (it dispatches a worker).

    def _model_info_text(self) -> str:
        s = self.state
        if s.backend_type == "cli":
            return (
                f"[b]Backend:[/b] CLI · {safe_markup(s.cli_type or '')}\n"
                "Discovery: Refresh to list models (if the CLI exposes them), or enter an id / "
                "use the CLI default."
            )
        conn = s.provider_name or "(new connection)"
        return (
            f"[b]Backend:[/b] API · {safe_markup(conn)}\n"
            "Discovery: the provider's models endpoint (best-effort). A model is required."
        )

    def _sync_model_fields(self) -> None:
        mode = self._radio_value("#model-mode", _MODEL_MODES) or "discovered"
        self.query_one("#model_select", Select).display = mode == "discovered"
        self.query_one("#model", Input).display = mode == "manual"
        # The catalog filters only make sense while browsing a discovered list.
        self.query_one("#catalog-filters").display = (
            mode == "discovered" and self.state.backend_type == "api"
        )
        is_api = self.state.backend_type == "api"
        preset = self._preset()
        self.query_one("#model-validate").display = is_api
        self.query_one("#open-model-page").display = bool(preset and preset.catalog_url)
        # The unverified override is only offered where the gate applies, and never pre-selected.
        self.query_one("#allow-unverified").display = bool(preset and preset.catalog_is_mixed)
        self.query_one("#override-row").display = bool(
            preset
            and preset.catalog_is_mixed
            and self.query_one("#allow-unverified", Checkbox).value
        )
        warning = self.query_one("#catalog-warning", Static)
        if preset and preset.catalog_is_mixed:
            warning.display = True
            warning.update(
                "NVIDIA's catalog contains chat, embedding, reranking, vision and other model "
                "types.\nA catalog entry is not automatically compatible with OpenAgent agents.\n"
                "Validate the selected model before creating the agent."
            )
        else:
            warning.display = False

    def _apply_catalog_filters(self) -> None:
        """Filter the already-fetched catalog locally — never a network call per keystroke (§14.2)."""

        search = self.query_one("#model-search", Input).value.strip() or None
        owner = selected_string(self.query_one("#model-owner", Select)) or None
        models = filter_models(self._catalog, search=search, owner=owner)
        select = self.query_one("#model_select", Select)
        select.set_options([(_catalog_label(m), m.id) for m in models])
        if not models:
            status = "[yellow]no catalog model matches these filters[/yellow]"
        else:
            status = (
                f"[green]{len(models)} of {len(self._catalog)} model(s)[/green]"
                " — pick one, then Validate Model & Key"
            )
        if self._catalog_error is not None:
            error_type, message = self._catalog_error
            status += (
                f"\n[yellow]partial catalog ({safe_markup(error_type)}): "
                f"{safe_markup(message, 200)}[/yellow]"
            )
        self._set_model_status(status)

    def _open_url(self, url: str) -> None:
        """Open a URL with Python's cross-platform webbrowser — never a shell command (§13.4)."""

        opened = False
        try:
            opened = webbrowser.open(url)
        except Exception:  # noqa: BLE001 - a headless/SSH box has no browser; that is not an error
            opened = False
        if opened:
            self.notify(f"opened {url}")
        else:
            # Fall back to showing a copyable address rather than pretending it worked.
            self._set_model_status(
                f"could not open a browser — copy this address: {safe_markup(url)}"
            )
            self.notify(f"could not open a browser — copy this address: {url}", severity="warning")

    def _validate_model(self) -> None:
        """Run a REAL capability probe against the selected model (§14.2, §15)."""

        if not self._capture_model(validate=True, gate=False):
            return
        model = self.state.model
        if not model:
            self._field_error("err-model", "choose or type a model first")
            return
        self.query_one("#model-verify", Static).update(
            f"[dim]validating {safe_markup(model)} — text, streaming and tool calling…[/dim]"
        )
        self.run_worker(self._do_validate(model), exclusive=True)

    async def _do_validate(self, model: str) -> None:
        oa = self.app.oa  # type: ignore[attr-defined]
        s = self.state
        try:
            if s.provider_mode == "existing" and s.provider_name:
                probe = await oa.providers.probe_model(s.provider_name, model, refresh=True)
            else:
                probe = await oa.providers.probe_model_config(
                    model_id=model,
                    provider_type=s.provider_type or "custom",
                    protocol=s.protocol,
                    base_url=s.base_url,
                    region=s.region,
                    workspace_id=s.workspace_id,
                    api_key=s.api_key.get_secret_value() if s.api_key else None,
                    key_env=s.key_env,
                )
        except Exception as exc:  # noqa: BLE001 - surface any failure honestly, never as success
            self._probe = None
            self.query_one("#model-verify", Static).update(
                f"[red]✗ could not validate: {safe_markup(str(exc), 200)}[/red]"
            )
            return
        self._probe = probe
        self.query_one("#model-verify", Static).update(_probe_line(probe))

    def _verification_note(self) -> str:
        probe = self._probe
        if probe is None:
            return "not validated"
        return (
            "verified agent compatible"
            if probe.agent_compatible
            else (f"NOT verified ({probe.category})")
        )

    def _refresh_models(self) -> None:
        self._set_model_status("[dim]discovering models…[/dim]")
        if self.state.backend_type == "cli":
            cli = self.state.cli_type
            if not cli:
                self._set_model_status("[red]choose a CLI first[/red]")
                return
            self.run_worker(self._do_discover_cli(cli, self.state.cli_executable), exclusive=True)
        else:
            self.run_worker(self._do_discover_api(), exclusive=True)

    async def _do_discover_cli(self, cli_type: str, executable: str | None) -> None:
        result = await discover_cli_models(cli_type, executable)
        self._apply_cli_models(result)

    def _apply_cli_models(self, result: CliModelDiscovery) -> None:
        select = self.query_one("#model_select", Select)
        if not result.available:
            # Honest: this CLI does not expose a model listing — keep manual + "CLI default".
            select.set_options([])
            self._set_model_status(
                f"[yellow]{safe_markup(result.error or 'this CLI does not expose model listing')}"
                "[/yellow] — enter an id manually, or use the CLI default"
            )
            return
        if not result.models:
            select.set_options([])
            self._set_model_status(
                "[yellow]no models reported — enter an id, or use the CLI default[/yellow]"
            )
            return
        self._model_method = result.method or ""
        if result.options:
            labels: list[tuple[str, str]] = []
            for option in result.options:
                verification = (
                    "credential verified"
                    if option.entitlement_verified
                    else (
                        "installed CLI advertised"
                        if option.source == "codex-app-server"
                        else "availability resolved at runtime"
                    )
                )
                labels.append(
                    (
                        f"{option.display_name} · {option.kind} · {option.source} · {verification}",
                        option.id,
                    )
                )
            select.set_options(labels)
        else:
            select.set_options([(m, m) for m in result.models])
        via = f" (via {safe_markup(result.method)})" if result.method else ""
        qualification = ""
        if result.cli_type == "claude":
            qualification = (
                " — Claude Code choices; account availability is resolved by Claude Code at runtime"
            )
        if result.partial and result.error:
            qualification += f" — partial: {safe_markup(result.error, 180)}"
        self._set_model_status(
            f"[green]found {len(result.models)} model(s){via}[/green] — pick one below"
            f"{qualification}"
        )

    async def _do_discover_api(self) -> None:
        oa = self.app.oa  # type: ignore[attr-defined]
        s = self.state
        try:
            if s.provider_mode == "existing" and s.provider_name:
                result = await oa.providers.remote_models(s.provider_name)
            else:
                result = await oa.providers.remote_models_config(
                    provider_type=s.provider_type or "custom",
                    protocol=s.protocol,
                    base_url=s.base_url,
                    region=s.region,
                    workspace_id=s.workspace_id,
                    api_key=s.api_key.get_secret_value() if s.api_key else None,
                    key_env=s.key_env,
                )
        except Exception as exc:  # noqa: BLE001 - discovery is best-effort
            self._set_model_status(
                f"[red]could not load models: {safe_markup(str(exc), 200)}[/red]"
            )
            self.query_one("#model_select", Select).set_options([])
            return
        self._apply_api_models(result)

    def _apply_api_models(self, result) -> None:
        if not isinstance(result, ModelDiscoveryResult):
            result = ModelDiscoveryResult(
                models=list(result), ok=True, partial=False, source="legacy-provider-adapter"
            )
        select = self.query_one("#model_select", Select)
        self._catalog = list(result.models)
        self._catalog_error = (
            (result.error_type or "unknown", result.error_message or "no detail")
            if not result.ok
            else None
        )
        if not self._catalog:
            select.set_options([])
            if result.ok:
                self._set_model_status(
                    "[yellow]the provider returned a valid empty catalog — enter a model id "
                    "manually below[/yellow]"
                )
            else:
                self._set_model_status(
                    f"[red]model discovery failed ({safe_markup(result.error_type or 'unknown')}): "
                    f"{safe_markup(result.error_message or 'no detail', 200)}[/red] — enter a "
                    "model id manually below"
                )
            return
        self._model_method = "provider models"
        # Populate the publisher filter from what the catalog actually reported (§14.2).
        owners = publishers(self._catalog)
        self.query_one("#model-owner", Select).set_options([(o, o) for o in owners])
        self._apply_catalog_filters()

    def _capture_model(self, *, validate: bool, gate: bool = True) -> bool:
        """Write the model choice into state.

        ``gate`` runs the mixed-catalog probe requirement (§14.3). It is skipped when the *Validate*
        button itself is capturing the selection — that would be circular: you cannot require a probe
        in order to run the probe.
        """

        mode = self._radio_value("#model-mode", _MODEL_MODES) or "discovered"
        if mode == "default":
            if self.state.backend_type != "cli":
                if validate:
                    self._field_error("err-model", "an API agent needs a model — pick or type one")
                    return False
                return True
            self.state.model = None  # CLI's own default; persisted as no pinned model (item 11)
            self._model_status = "default"
            return True
        if mode == "manual":
            model = self._input("model") or None
            if validate and not model:
                self._field_error("err-model", "type a model id, or choose another option")
                return False
            self.state.model = model
            # A hand-typed id is never "discovered" and never implies verification (§14.4).
            self._model_status = "manual"
            return not (validate and gate) or self._model_is_allowed(model)
        # discovered
        chosen = selected_string(self.query_one("#model_select", Select))
        if validate and not chosen:
            self._field_error("err-model", "pick a discovered model, or switch to manual/default")
            return False
        self.state.model = chosen
        self._model_status = "discovered"
        return not (validate and gate) or self._model_is_allowed(chosen)

    def _model_is_allowed(self, model: str | None) -> bool:
        """Gate a mixed-catalog model on a real probe (§14.3, §15.2-§15.4).

        Scoped to catalogs that mix model types (NVIDIA Build): there a model id proves nothing, so
        continuing without a probe would build an agent that may simply not work. The user can always
        tick the explicit, never-preselected override.
        """

        preset = self._preset()
        if not preset or not preset.catalog_is_mixed or not model:
            return True
        if self.query_one("#allow-unverified", Checkbox).value:
            reason = self._input("override-reason")
            if not reason:
                self._field_error("err-model", "an explicit override reason is required")
                return False
            self.state.model_override_reason = reason
            return True  # explicit advanced override — surfaced loudly on Review
        self.state.model_override_reason = None
        probe = self._probe
        if probe is None or probe.model != model:
            hint = (
                " This model id looks like it may not be a chat model."
                if looks_non_chat(model)
                else ""
            )
            self._field_error(
                "err-model",
                f"{model} has not been validated — press 'Validate Model & Key' first.{hint}",
            )
            return False
        if not probe.agent_compatible:
            self._field_error("err-model", probe.message())
            return False
        return True

    # ------------------------------------------------------------------ review + create

    def _render_review(self) -> None:
        s = self.state
        if s.backend_type == "cli":
            backend = f"CLI · {safe_markup(s.cli_type or '')}"
        else:
            backend = f"API · {safe_markup(s.provider_name or '(new connection)')} ({s.provider_mode or 'new'})"
        model = safe_markup(s.model) if s.model else "(CLI default — no pinned model)"
        status = {
            "discovered": "discovered",
            "manual": "manual — not verified",
            "default": "CLI default",
        }.get(self._model_status, self._model_status)
        method = (
            f" · via {safe_markup(self._model_method)}"
            if (s.model and self._model_method and self._model_status == "discovered")
            else ""
        )
        lines = [
            f"[b]Backend:[/b] {backend}",
            f"[b]Model:[/b] {model}  [dim]({status}{method})[/dim]",
            f"[b]Name:[/b] {safe_markup(s.agent_name or '(unset)')}",
            f"[b]Profile:[/b] {safe_markup(s.permission_profile)}",
            f"[b]Max steps:[/b] {s.max_steps}",
        ]
        if s.tags:
            lines.append(f"[b]Tags:[/b] {safe_markup(', '.join(s.tags))}")
        if s.backend_type == "api":
            # State the verification outcome plainly, and warn loudly when it is not verified —
            # an override must never look like a normal success (§15.3).
            lines.append(f"[b]Validation:[/b] {safe_markup(self._verification_note())}")
            probe = self._probe
            if probe is not None and probe.agent_compatible:
                lines.append("[green]✓ Verified Agent Compatible[/green]")
            elif self._preset() and self._preset().catalog_is_mixed:  # type: ignore[union-attr]
                lines.append(
                    "[b][yellow]⚠ WARNING — this model was NOT verified agent-compatible.[/yellow][/b]\n"
                    "[yellow]It may answer questions but fail to operate OpenAgent tools.[/yellow]"
                )
            if s.model_override_reason:
                lines.append(f"[b]Override reason:[/b] {safe_markup(s.model_override_reason, 500)}")
        self.query_one("#review-card", Static).update("\n".join(lines))

    def action_create(self) -> None:
        if self.step not in _FINAL_STEPS:
            return
        self._clear_errors()
        try:
            self._create()
        except _EXPECTED_ERRORS as exc:
            self._summary([str(exc)])
        finally:
            self._clear_secret_widget()

    def _create(self) -> None:
        oa = self.app.oa  # type: ignore[attr-defined]
        s = self.state
        common = {
            "title": s.title,
            "description": s.description,
            "tags": s.tags,
            "system_prompt": s.system_prompt,
            "permission_profile": s.permission_profile,
            "max_steps": s.max_steps,
            "model_override_reason": s.model_override_reason,
        }
        if s.backend_type == "cli":
            agent = oa.agents.create(
                name=s.agent_name,
                runtime_type=RuntimeType.CLI,
                cli=s.cli_type,
                model=s.model,
                **common,
            )
        elif s.provider_mode == "existing":
            agent = oa.agents.create(
                name=s.agent_name,
                runtime_type=RuntimeType.API_AGENT,
                provider=s.provider_name,
                model=s.model,
                **common,
            )
        else:
            agent = self._create_with_new_connection(common)
            if agent is None:
                return
        self._clear_secret_widget()
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
                provider_name=s.provider_name,
                provider_type=s.provider_type,
                protocol=s.protocol,
                base_url=s.base_url,
                region=s.region,
                workspace_id=s.workspace_id,
                api_key=s.api_key.get_secret_value() if s.api_key else None,
                key_env=s.key_env,
                credential_source=s.credential_source,
                model=s.model,
                name=s.agent_name,
                **common,
            )
        except ProviderValidationError as exc:
            # A credential/connection problem sends the user back to the connection step to fix it.
            if exc.field in ("api_key", "key_env"):
                self._show_step("connection")
                self.query_one("#conn-status", Static).update(
                    f"[red]✗ {safe_markup(str(exc), 200)}[/red]"
                )
                self._field_error("err-conn", str(exc))
            self._summary([str(exc)])
            return None

    # ------------------------------------------------------------------ live sync / details

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        rid = event.radio_set.id
        if rid == "backend":
            picked = self._radio_value("#backend", self._backend_values)
            new_backend: BackendType = "api" if picked == "api" else "cli"
            if new_backend != self.state.backend_type:
                self._clear_secret_widget()
            self.state.set_backend(new_backend)
        elif rid == "cli":
            self._sync_cli_detail()
        elif rid == "provider":
            new = self._radio_value("#provider", self._preset_values)
            if new and new != self.state.provider_type:
                # A different provider invalidates the connection/credential/model — clear both the
                # state and the widgets so nothing stale from the previous provider is submitted.
                self.state.set_provider_type(new)
                self._reset_connection_widgets()
                self._clear_secret_widget()
            self._sync_provider_detail()
        elif rid == "cred":
            # A different credential source invalidates whatever was typed for the previous one.
            self._clear_secret_widget()
            self._sync_connection_fields()
        elif rid == "conn-mode":
            self._clear_secret_widget()
            self._sync_connection_fields()
        elif rid == "model-mode":
            self._sync_model_fields()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "model_select":
            chosen = selected_string(event.select)
            if chosen:
                # Mirror the pick into the manual field so switching modes keeps the value visible.
                self.query_one("#model", Input).value = chosen
                # A new selection invalidates the previous model's probe (§16): a "verified" badge
                # must never carry over to a model that was not the one tested.
                if self._probe is not None and self._probe.model != chosen:
                    self._probe = None
                    self.query_one("#model-verify", Static).update("")
        elif event.select.id == "model-owner":
            self._apply_catalog_filters()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "allow-unverified":
            self.query_one("#override-row").display = event.value
            if not event.value:
                self.query_one("#override-reason", Input).value = ""
                self.state.model_override_reason = None

    def on_input_changed(self, event: Input.Changed) -> None:
        # Local, instant filtering — no network call per keystroke (§14.2).
        if event.input.id == "model-search":
            self._apply_catalog_filters()

    def _sync_cli_detail(self) -> None:
        entry = self._entry_for(self._radio_value("#cli", self._cli_values))
        self.query_one("#cli-detail", Static).update(_cli_detail_text(entry))

    def _sync_provider_detail(self) -> None:
        name = self._radio_value("#provider", self._preset_values)
        self.query_one("#provider-detail", Static).update(_provider_detail_text(name))

    def _reset_connection_widgets(self) -> None:
        """Clear the connection-step widgets, **including the password field** (part 19).

        Clearing only ``state.api_key`` left the typed key sitting in the ``#api_key`` Input, so it
        was re-read on the next capture and submitted against a different provider. The widget and
        the state are cleared together, always.
        """

        for wid in (
            "conn_name",
            "base_url",
            "region",
            "workspace_id",
            "api_key",
            "key_env",
            "model",
        ):
            try:
                self.query_one(f"#{wid}", Input).value = ""
            except Exception:  # pragma: no cover - widget always present
                pass
        self.query_one("#model_select", Select).set_options([])
        self.state.api_key = None
        self.state.model = None
        self._model_context = None
        self._catalog = []
        self._catalog_error = None
        self._status("")

    def _clear_secret_widget(self) -> None:
        """Wipe the API-key field and the in-memory secret together."""

        self.clear_secret_material()

    def _clear_secret_state(self) -> None:
        self.state.clear_secret()

    def _preset(self) -> ProviderPreset | None:
        return get_preset(self.state.provider_type or "")

    def _sync_connection_fields(self) -> None:
        mode = self._radio_value("#conn-mode", ["new", "existing"]) or "new"
        is_new = mode == "new"
        self.query_one("#conn-new").display = is_new
        self.query_one("#conn-existing").display = not is_new
        cred = self._radio_value("#cred", self._cred_values) or "keychain"
        self.query_one("#key-row").display = is_new and cred == "keychain"
        self.query_one("#env-row").display = is_new and cred == "env"
        if is_new:
            self._sync_hosted_fields()
        if not is_new:
            self._populate_existing_connections()

    def _sync_hosted_fields(self) -> None:
        """Make the Connection step provider-aware (§13.1).

        For a hosted catalog with published metadata (NVIDIA Build) the endpoint and protocol are
        fixed facts, the key source defaults to the OS keychain, the env-var name is pre-filled, and
        'No API key' is not offered at all — it is never a legitimate choice for a provider that
        requires one.
        """

        preset = self._preset()
        hosted = bool(preset and preset.catalog_url)
        self.query_one("#hosted-info").display = hosted
        self.query_one("#hosted-actions").display = hosted
        self.query_one("#hosted-hint").display = hosted
        # A provider that needs a key must not offer "no key" (§13.1).
        none_button = self.query_one("#cred-none", RadioButton)
        none_button.display = not (preset and preset.needs_key)
        if not preset or not hosted:
            # Only a hosted-catalog provider gets pre-filled defaults; for everything else the fields
            # stay exactly as the user left them (switching provider clears them — part 19).
            return
        if hosted:
            endpoint = preset.openai_base_url or ""
            self.query_one("#hosted-info", Static).update(
                f"[b]{safe_markup(preset.label)}[/b]\n"
                f"Hosted NVIDIA NIM endpoints from build.nvidia.com.\n"
                f"Use one NVIDIA API key to access available catalog models.\n"
                f"[b]Official endpoint:[/b] {safe_markup(endpoint)}\n"
                f"[b]Protocol:[/b] {safe_markup(preset.protocol.value)}"
            )
            self.query_one("#hosted-hint", Static).update(
                "[b]How to get an API key[/b]\n"
                "1. Sign in to NVIDIA Build.\n"
                "2. Open a model page.\n"
                "3. Click Generate API Key / Get API Key.\n"
                f"4. Paste the key here, or save it as {safe_markup(preset.default_env_var or '')}.\n"
                "5. Never put the key directly in a command.\n"
                + (
                    f"[dim]{safe_markup(preset.credential_hint or '')}[/dim]"
                    if preset.credential_hint
                    else ""
                )
            )
        # Sensible provider-aware defaults the user can still override.
        name_input = self.query_one("#conn_name", Input)
        if not name_input.value.strip():
            name_input.value = preset.provider_type
        env_input = self.query_one("#key_env", Input)
        if preset.default_env_var and not env_input.value.strip():
            env_input.value = preset.default_env_var

    def _compatible_connections(self) -> list[str]:
        """Saved connections that belong to the **selected provider family** (part 19).

        Offering an Anthropic provider card a `deepseek-main` connection produced an agent that could
        not work: the protocol, base URL and key all belong to a different service. The list is
        filtered to the family the user picked, so an incompatible pairing is not offered at all.
        """

        oa = self.app.oa  # type: ignore[attr-defined]
        # The *committed* provider choice wins: by the time the connection step is shown, the family
        # has been captured into state, and that is what the connection will actually be created for.
        family = self.state.provider_type or self._radio_value("#provider", self._preset_values)
        return [p.name for p in oa.providers.list() if not family or p.provider_type == family]

    def _populate_existing_connections(self) -> None:
        names = self._compatible_connections()
        select = self.query_one("#existing-provider", Select)
        select.set_options([(n, n) for n in names])
        family = self.state.provider_type or "this provider"
        if names:
            self._field_error("err-provider", "")
        else:
            self._field_error(
                "err-provider",
                f"no saved connection for {family} — create a new one instead",
            )

    # ------------------------------------------------------------------ workers (test connection)

    def _new_conn_params(self) -> dict:
        # Read the live connection widgets for a transient test (never persisted).
        protocol_val = selected_string(self.query_one("#protocol", Select))
        protocol = None if protocol_val in (None, "preset") else Protocol(protocol_val)
        cred = self._radio_value("#cred", self._cred_values) or "keychain"
        return {
            "provider_type": self._radio_value("#provider", self._preset_values) or "custom",
            "protocol": protocol,
            "base_url": self._input("base_url") or None,
            "region": self._input("region") or None,
            "workspace_id": self._input("workspace_id") or None,
            "api_key": (self.query_one("#api_key", Input).value or None)
            if cred == "keychain"
            else None,
            "key_env": self._input("key_env") or None if cred == "env" else None,
        }

    def _test_connection(self) -> None:
        self._status("[dim]testing…[/dim]")
        self.run_worker(self._do_test(self._new_conn_params()), exclusive=True)

    async def _do_test(self, p: dict) -> None:
        oa = self.app.oa  # type: ignore[attr-defined]
        try:
            result = await oa.providers.test_config(
                provider_type=p["provider_type"],
                protocol=p["protocol"],
                base_url=p["base_url"],
                region=p["region"],
                workspace_id=p["workspace_id"],
                api_key=p["api_key"],
                key_env=p["key_env"],
            )
        except Exception as exc:  # noqa: BLE001 - surface any failure as an unhealthy result
            self._status(f"[red]✗ {safe_markup(str(exc), 200)}[/red]")
            return
        self._status(
            f"[green]✓ connection ok[/green] — {safe_markup(result.detail, 200)}"
            if result.ok
            else f"[red]✗ {safe_markup(result.detail, 200)}[/red]"
        )

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

    def _set_model_status(self, message: str) -> None:
        self.query_one("#model-status", Static).update(message)

    def _field_error(self, wid: str, message: str) -> None:
        self.query_one(f"#{wid}", Label).update(f"✗ {message}" if message else "")

    def _clear_errors(self) -> None:
        for wid in ("err-name", "err-cli", "err-provider", "err-conn", "err-model", "err-steps"):
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
    lines = [f"[b]{p.label}[/b]"]
    if p.catalog_url:
        # A hosted catalog card states plainly what it is and what one key buys (§13).
        lines.append("Hosted NVIDIA NIM endpoints from build.nvidia.com.")
        lines.append("Use one NVIDIA API key to access available catalog models.")
    lines.append(f"Protocol: {p.protocol.value}")
    if p.openai_base_url:
        lines.append(f"Endpoint: {p.openai_base_url}")
    lines.append(f"{where} · {key}")
    if p.catalog_is_mixed:
        lines.append(
            "Catalog mixes model types — a listing is not a capability claim; probe first."
        )
    else:
        lines.append(
            "Model discovery: supported (best-effort) · Compatibility: adapter "
            "implemented, live-unverified"
        )
    return "\n".join(lines)


def _catalog_label(model: RemoteModel) -> str:
    """A catalog row: id, publisher, and an honest *hint* when it may not be a chat model (§14.3)."""

    parts = [model.id]
    if model.owned_by:
        parts.append(f"· {model.owned_by}")
    if looks_non_chat(model.id):
        parts.append("· may not be a chat model")
    return "  ".join(parts)


def _probe_line(probe: AgentModelProbe) -> str:
    """Render a probe verdict — only capabilities that were actually observed (§15.2)."""

    mark = {True: "[green]yes[/green]", False: "[red]no[/red]", None: "[yellow]unverified[/yellow]"}
    caps = probe.capabilities
    colour = "green" if probe.agent_compatible else "yellow"
    return (
        f"text {mark[caps.text]} · streaming {mark[caps.streaming]} · "
        f"tool calling {mark[caps.tool_calling]}\n"
        f"[{colour}]{safe_markup(probe.message())}[/{colour}]"
    )


def _parse_steps(value: str) -> tuple[int, str]:
    """Parse ``max_steps``. Returns ``(steps, error)``; an invalid value is never silently accepted."""

    text = (value or "").strip()
    if not text:
        return 40, ""
    try:
        steps = int(text)
    except (TypeError, ValueError):
        return 40, f"must be a whole number between {MIN_STEPS} and {MAX_STEPS}"
    if not (MIN_STEPS <= steps <= MAX_STEPS):
        return 40, f"must be between {MIN_STEPS} and {MAX_STEPS}"
    return steps, ""
