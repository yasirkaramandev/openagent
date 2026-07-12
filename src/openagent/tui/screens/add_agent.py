"""Add-agent wizard (spec §31 add-agent flow).

A responsive, keyboard-first form:

* pick a **runtime** (API or CLI) first; only the relevant fields are then shown;
* **CLI agents** pick an installed CLI (labelled installed / not installed);
* **API agents** get a *unified* connection journey — either reuse a saved provider connection, or
  connect a brand-new API (provider preset · name · protocol · optional base URL/region/workspace ·
  credential source · masked key · Test Connection · Load Models) — all without leaving Add Agent.
  The provider connection stores the credential; the agent only references it by name. The API key
  is never written to the agent record, OPENAGENT.md, or logs;
* common identity fields (name, title, description, tags, permission profile, optional system prompt)
  live in a **scrollable** container; a **fixed bottom action bar** keeps *Create Agent* visible;
* every ``Select`` empty state is normalised through :func:`selected_string`, so a Textual sentinel
  (``Select.NULL`` / ``Select.BLANK`` / ``NoSelection``) can never reach a service or Pydantic model;
* expected validation/credential errors appear inline and keep the form open with values intact;
  the TUI never shows a raw traceback for a recoverable form error.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, Select, Static, TextArea

from ...core.models import Protocol, RuntimeType
from ...core.permissions import profile_names
from ...credentials.store import CredentialError
from ...providers.factory import PRESETS, preset_names
from ...runtimes.cli.registry import cli_install_status
from ...services.agent_service import AgentError
from ...services.provider_service import ProviderValidationError
from ..select_utils import selected_string

try:  # keyring errors are environment-dependent; treat them as expected/recoverable when present
    from keyring.errors import KeyringError as _KeyringError
except Exception:  # pragma: no cover - keyring optional
    _KeyringError = ()  # type: ignore[assignment,misc]

#: Expected, recoverable errors shown inline in the form (never as a full-screen traceback).
#: Anything *not* listed here is a genuine bug and is allowed to propagate so tests still fail.
_EXPECTED_ERRORS: tuple[type[BaseException], ...] = (AgentError, CredentialError, ValueError)
if isinstance(_KeyringError, type):
    _EXPECTED_ERRORS = (*_EXPECTED_ERRORS, _KeyringError)

_CRED_SOURCES = [
    ("OS keychain (recommended)", "keychain"),
    ("Environment variable", "env"),
    ("No API key (local provider)", "none"),
]
_PROTOCOLS = [
    ("(preset default)", "preset"),
    ("openai-chat", Protocol.OPENAI_CHAT.value),
    ("openai-responses", Protocol.OPENAI_RESPONSES.value),
    ("anthropic-messages", Protocol.ANTHROPIC_MESSAGES.value),
]
_API_MODES = [
    ("Use an existing connection", "existing"),
    ("Connect a new API", "new"),
]


class AddAgentScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+s", "create", "Create Agent"),
        Binding("f10", "create", "Create Agent"),
    ]

    DEFAULT_CSS = """
    AddAgentScreen #form { height: 1fr; padding: 0 2; }
    AddAgentScreen Label { margin: 1 0 0 0; text-style: bold; }
    AddAgentScreen .hint { color: $text-muted; text-style: none; margin: 0 0 1 0; height: auto; }
    AddAgentScreen .field-error { color: $error; text-style: none; margin: 0; height: auto; }
    AddAgentScreen #error-summary { color: $error; padding: 0 2; height: auto; }
    AddAgentScreen #conn-status { height: auto; margin: 1 0 0 0; }
    AddAgentScreen #system_prompt { height: 5; border: round $primary; }
    AddAgentScreen #action-bar {
        height: 3; padding: 0 2; align-horizontal: left; background: $panel;
    }
    AddAgentScreen #action-bar Button { margin: 0 2 0 0; }
    AddAgentScreen #conn-actions { height: auto; margin: 1 0 0 0; }
    AddAgentScreen #conn-actions Button { margin: 0 2 0 0; }
    """

    def compose(self) -> ComposeResult:
        oa = self.app.oa  # type: ignore[attr-defined]
        self._provider_names = [p.name for p in oa.providers.list()]
        providers = [(n, n) for n in self._provider_names]
        profiles = [(p, p) for p in profile_names()]
        presets = [(f"{PRESETS[name].label} ({name})", name) for name in preset_names()]
        # Default to the "existing" path only when there is something to reuse.
        default_mode = "existing" if self._provider_names else "new"

        yield Header()
        yield Static("Add Agent", classes="screen-title")
        with VerticalScroll(id="form"):
            yield Label("Runtime")
            yield Select([("API Agent", "api"), ("CLI Agent", "cli")], value="api",
                         id="runtime", allow_blank=False)

            # ---- CLI runtime ------------------------------------------------
            with Vertical(id="cli-group"):
                yield Label("CLI")
                yield Select(self._cli_options(), id="cli", allow_blank=True, prompt="Choose a CLI")
                yield Label("", id="err-cli", classes="field-error")

            # ---- API runtime ------------------------------------------------
            with Vertical(id="api-group"):
                yield Static(
                    "An API connection stores the provider credentials.\n"
                    "The agent defines how that connection will be used.",
                    classes="hint",
                )
                yield Label("API connection")
                yield Select(_API_MODES, value=default_mode, id="api-mode", allow_blank=False)

                # Existing-connection path
                with Vertical(id="api-existing"):
                    yield Label("Provider connection")
                    yield Select(providers, id="provider", allow_blank=True,
                                 prompt="select a saved connection")
                    yield Label("", id="err-provider", classes="field-error")

                # New-connection path (persisted through ProviderService on Create)
                with Vertical(id="api-new"):
                    yield Label("Provider")
                    yield Select(presets, value="openai", id="preset", allow_blank=False)
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
                    yield Select(_CRED_SOURCES, value="keychain", id="cred", allow_blank=False)
                    with Vertical(id="env-row"):
                        yield Label("Environment variable name")
                        yield Input(placeholder="e.g. DEEPSEEK_API_KEY", id="key_env")
                    with Vertical(id="key-row"):
                        yield Label("API key (hidden; stored in the OS keychain)")
                        yield Input(placeholder="paste key", password=True, id="api_key")

                # Model (shared by both API paths)
                yield Label("Model")
                yield Select([], id="model_select", allow_blank=True,
                             prompt="load models, or type an id below")
                yield Input(placeholder="remote model id, e.g. deepseek-chat", id="model")
                yield Label("", id="err-model", classes="field-error")
                with Horizontal(id="conn-actions"):
                    yield Button("Test Connection", id="test")
                    yield Button("Load Models", id="load-models")
                yield Static("", id="conn-status")

            # ---- common identity fields ------------------------------------
            yield Label("Name")
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
            yield Label("System prompt (optional)")
            yield TextArea(id="system_prompt")
            yield Static("", id="error-summary")

        with Horizontal(id="action-bar"):
            yield Button("Create Agent", variant="success", id="create")
            yield Button("Back (Esc)", id="back")
        yield Footer()

    def on_mount(self) -> None:
        self._sync_runtime_fields()
        self.query_one("#name", Input).focus()

    # ------------------------------------------------------------------ conditional fields

    def _cli_options(self) -> list[tuple[str, str]]:
        options: list[tuple[str, str]] = []
        for cli_type, installed in cli_install_status():
            status = "installed" if installed else "not installed"
            options.append((f"{cli_type} — {status}", cli_type))
        return options

    def on_select_changed(self, event: Select.Changed) -> None:
        sid = event.select.id
        if sid == "runtime":
            self._sync_runtime_fields()
        elif sid in ("api-mode", "cred"):
            self._sync_api_fields()
        elif sid == "model_select":
            # The Select is a convenience populator; the Model input is the single source of truth.
            chosen = selected_string(event.select)
            if chosen:
                self.query_one("#model", Input).value = chosen

    def _sync_runtime_fields(self) -> None:
        is_cli = selected_string(self.query_one("#runtime", Select)) == "cli"
        self.query_one("#cli-group").display = is_cli
        self.query_one("#api-group").display = not is_cli
        if not is_cli:
            self._sync_api_fields()

    def _sync_api_fields(self) -> None:
        mode = selected_string(self.query_one("#api-mode", Select))
        is_new = mode == "new"
        self.query_one("#api-existing").display = not is_new
        self.query_one("#api-new").display = is_new
        # Test Connection only makes sense for a not-yet-saved connection.
        self.query_one("#test").display = is_new
        cred = selected_string(self.query_one("#cred", Select))
        self.query_one("#env-row").display = is_new and cred == "env"
        self.query_one("#key-row").display = is_new and cred == "keychain"

    # ------------------------------------------------------------------ actions

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "create":
            self.action_create()
        elif event.button.id == "back":
            self.app.pop_screen()
        elif event.button.id == "test":
            self._test_connection()
        elif event.button.id == "load-models":
            self._load_models()

    def action_create(self) -> None:
        self._clear_errors()
        try:
            self._create()
        except _EXPECTED_ERRORS as exc:
            # Recoverable form/config error: keep the screen open, show it inline, preserve inputs.
            self._summary([str(exc)])

    def _create(self) -> None:
        oa = self.app.oa  # type: ignore[attr-defined]
        runtime = selected_string(self.query_one("#runtime", Select))
        name = self._input("name")
        errors: list[str] = []
        if not name:
            errors.append("name is required")
            self._field_error("err-name", "required")

        common = {
            "title": self._input("title"), "description": self._input("description"),
            "tags": [t.strip() for t in self._input("tags").split(",") if t.strip()],
            "system_prompt": self.query_one("#system_prompt", TextArea).text.strip(),
            "permission_profile": selected_string(self.query_one("#profile", Select)) or "safe-edit",
        }

        if runtime == "cli":
            cli = selected_string(self.query_one("#cli", Select))
            if not cli:
                errors.append("choose a CLI")
                self._field_error("err-cli", "Choose a CLI")
            if errors:
                return self._summary(errors)
            agent = oa.agents.create(name=name, runtime_type=RuntimeType.CLI, cli=cli, **common)
        else:
            # Validate every required field first so a missing model never leaves a persisted
            # (but unused) provider connection behind.
            model = self._input("model")
            if not model:
                errors.append("API agents need a model id")
                self._field_error("err-model", "required")
            mode = selected_string(self.query_one("#api-mode", Select))
            if mode == "new":
                agent = self._create_with_new_connection(name, model, common, errors)
                if agent is None:
                    return self._summary(errors)
            else:
                provider = self._resolve_api_provider(errors)
                if errors:
                    return self._summary(errors)
                agent = oa.agents.create(
                    name=name, runtime_type=RuntimeType.API_AGENT, provider=provider,
                    model=model, **common,
                )

        self.notify(f"agent '{agent.name}' created — OPENAGENT.md updated", severity="information")
        # Return to the Agents list, which reloads and now shows the new agent.
        self.app.pop_screen()
        self.app.open_section("agents")  # type: ignore[attr-defined]

    def _resolve_api_provider(self, errors: list[str]) -> str | None:
        """Return the *existing* saved provider-connection name to bind.

        The new-connection path is handled atomically by :meth:`_create_with_new_connection`.
        """

        provider = selected_string(self.query_one("#provider", Select))
        if not provider:
            errors.append("choose a provider connection (or connect a new API)")
            self._field_error("err-provider", "required")
        return provider

    def _create_with_new_connection(
        self, name: str, model: str, common: dict, errors: list[str]
    ):
        """Connect a new provider *and* create the agent as one atomic transaction (item 3).

        On any failure — invalid credential, duplicate name, OPENAGENT.md write error — nothing is
        left behind (no provider row, no keychain secret, no half-written doc); the error is shown
        inline and the form stays open. Returns the created agent, or ``None`` on failure.
        """

        oa = self.app.oa  # type: ignore[attr-defined]
        p = self._new_conn_params()
        if not p["name"]:
            errors.append("connection name is required")
            self._field_error("err-conn", "required")
            return None
        if oa.providers.get(p["name"]):
            errors.append(f"a provider named {p['name']!r} already exists — pick 'existing' instead")
            self._field_error("err-conn", "already exists")
            return None
        if errors:  # e.g. missing model — don't persist a connection we're about to reject
            return None
        try:
            agent = oa.agents.create_with_new_provider(
                provider_name=p["name"], provider_type=p["provider_type"], protocol=p["protocol"],
                base_url=p["base_url"], region=p["region"], workspace_id=p["workspace_id"],
                api_key=p["api_key"], key_env=p["key_env"], credential_source=p["cred"],
                model=model, name=name, **common,
            )
        except ProviderValidationError as exc:
            errors.append(str(exc))
            self._status(f"[red]✗ {exc}[/red]")
            self._field_error("err-conn", "credential invalid")
            return None
        except _EXPECTED_ERRORS as exc:
            errors.append(str(exc))
            self._field_error("err-name", "could not create")
            return None
        self.notify(f"provider '{p['name']}' connected", severity="information")
        return agent

    def _test_connection(self) -> None:
        p = self._new_conn_params()
        self._status("[dim]testing…[/dim]")
        self.run_worker(self._do_test(p), exclusive=True)

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
        if result.ok:
            self._status(f"[green]✓ connection ok[/green] — {result.detail}")
        else:
            self._status(f"[red]✗ {result.detail}[/red]")

    def _load_models(self) -> None:
        self._status("[dim]loading models…[/dim]")
        mode = selected_string(self.query_one("#api-mode", Select))
        if mode == "new":
            self.run_worker(self._load_models_new(self._new_conn_params()), exclusive=True)
        else:
            provider = selected_string(self.query_one("#provider", Select))
            if not provider:
                self._status("[red]select a provider connection first[/red]")
                return
            self.run_worker(self._load_models_existing(provider), exclusive=True)

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

    def _new_conn_params(self) -> dict:
        protocol_val = selected_string(self.query_one("#protocol", Select))
        protocol = None if protocol_val in (None, "preset") else Protocol(protocol_val)
        cred = selected_string(self.query_one("#cred", Select))
        return {
            "name": self._input("conn_name"),
            "provider_type": selected_string(self.query_one("#preset", Select)) or "custom",
            "protocol": protocol,
            "base_url": self._input("base_url") or None,
            "region": self._input("region") or None,
            "workspace_id": self._input("workspace_id") or None,
            "cred": cred,
            "api_key": (self.query_one("#api_key", Input).value or None) if cred == "keychain" else None,
            "key_env": self._input("key_env") or None if cred == "env" else None,
        }

    def _input(self, wid: str) -> str:
        return self.query_one(f"#{wid}", Input).value.strip()

    def _status(self, message: str) -> None:
        self.query_one("#conn-status", Static).update(message)

    def _field_error(self, wid: str, message: str) -> None:
        self.query_one(f"#{wid}", Label).update(f"✗ {message}")

    def _clear_errors(self) -> None:
        for wid in ("err-name", "err-cli", "err-provider", "err-conn", "err-model"):
            self.query_one(f"#{wid}", Label).update("")
        self.query_one("#error-summary", Static).update("")

    def _summary(self, errors: list[str]) -> None:
        self.query_one("#error-summary", Static).update(
            "[b]Cannot create agent:[/b]\n" + "\n".join(f"  • {e}" for e in errors)
        )
