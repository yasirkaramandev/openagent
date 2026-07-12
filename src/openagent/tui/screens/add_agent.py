"""Add-agent wizard (spec §31 add-agent flow).

A responsive, keyboard-first form:

* pick a **runtime** (API or CLI) first; only the relevant fields are then shown — CLI selection for
  CLI agents (labelled with installed/not-installed status), provider + model id for API agents;
* common identity fields (name, title, description, tags, permission profile, optional system prompt)
  live in a **scrollable** container so nothing overflows off-screen at 80×24;
* a **fixed bottom action bar** keeps *Create Agent* always visible;
* ``Ctrl+S`` / ``F10`` create, ``Esc`` goes back, ``Tab`` / ``Shift+Tab`` navigate;
* validation errors appear next to the offending field *and* in a summary, and the form stays open
  on failure. On success the new agent is saved (OPENAGENT.md is refreshed by the service) and the
  Agents list — now including it — is shown.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, Select, Static, TextArea

from ...core.models import RuntimeType
from ...core.permissions import profile_names
from ...runtimes.cli.base import find_executable
from ...runtimes.cli.registry import known_cli_types
from ...services.agent_service import AgentError


class AddAgentScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+s", "create", "Create Agent"),
        Binding("f10", "create", "Create Agent"),
    ]

    DEFAULT_CSS = """
    AddAgentScreen #form { height: 1fr; padding: 0 2; }
    AddAgentScreen Label { margin: 1 0 0 0; text-style: bold; }
    AddAgentScreen .field-error { color: $error; text-style: none; margin: 0; height: auto; }
    AddAgentScreen #error-summary { color: $error; padding: 0 2; height: auto; }
    AddAgentScreen #system_prompt { height: 5; border: round $primary; }
    AddAgentScreen #action-bar {
        height: 3; padding: 0 2; align-horizontal: left; background: $panel;
    }
    AddAgentScreen #action-bar Button { margin: 0 2 0 0; }
    """

    def compose(self) -> ComposeResult:
        oa = self.app.oa  # type: ignore[attr-defined]
        providers = [(p.name, p.name) for p in oa.providers.list()]
        profiles = [(p, p) for p in profile_names()]

        yield Header()
        yield Static("Add Agent", classes="screen-title")
        with VerticalScroll(id="form"):
            yield Label("Runtime")
            yield Select([("API Agent", "api"), ("CLI Agent", "cli")], value="api",
                         id="runtime", allow_blank=False)

            with Vertical(id="cli-group"):
                yield Label("CLI")
                yield Select(self._cli_options(), id="cli", allow_blank=True, prompt="select CLI")
                yield Label("", id="err-cli", classes="field-error")

            with Vertical(id="api-group"):
                yield Label("Provider")
                yield Select(providers, id="provider", allow_blank=True,
                             prompt="select provider (add one under Providers)")
                yield Label("", id="err-provider", classes="field-error")
                yield Label("Model ID")
                yield Input(placeholder="remote model id, e.g. deepseek-chat", id="model")
                yield Label("", id="err-model", classes="field-error")

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
        for cli_type in known_cli_types():
            installed = find_executable(cli_type) is not None
            status = "installed" if installed else "not installed"
            options.append((f"{cli_type} — {status}", cli_type))
        return options

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "runtime":
            self._sync_runtime_fields()

    def _sync_runtime_fields(self) -> None:
        is_cli = self._value("runtime") == "cli"
        self.query_one("#cli-group").display = is_cli
        self.query_one("#api-group").display = not is_cli

    # ------------------------------------------------------------------ actions

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "create":
            self.action_create()
        elif event.button.id == "back":
            self.app.pop_screen()

    def action_create(self) -> None:
        self._clear_errors()
        oa = self.app.oa  # type: ignore[attr-defined]
        runtime = self._value("runtime")
        name = self._input("name")
        errors: list[str] = []
        if not name:
            errors.append("name is required")
            self._field_error("err-name", "required")

        tags = [t.strip() for t in self._input("tags").split(",") if t.strip()]
        profile = self._value("profile") or "safe-edit"
        system_prompt = self.query_one("#system_prompt", TextArea).text.strip()
        common = {
            "title": self._input("title"), "description": self._input("description"),
            "tags": tags, "system_prompt": system_prompt, "permission_profile": profile,
        }

        try:
            if runtime == "cli":
                cli = self._value("cli")
                if not cli:
                    errors.append("choose a CLI")
                    self._field_error("err-cli", "choose a CLI")
                if errors:
                    return self._summary(errors)
                agent = oa.agents.create(name=name, runtime_type=RuntimeType.CLI, cli=cli, **common)
            else:
                provider = self._value("provider")
                model = self._input("model")
                if not provider:
                    errors.append("API agents need a provider")
                    self._field_error("err-provider", "required")
                if not model:
                    errors.append("API agents need a model id")
                    self._field_error("err-model", "required")
                if errors:
                    return self._summary(errors)
                agent = oa.agents.create(
                    name=name, runtime_type=RuntimeType.API_AGENT, provider=provider,
                    model=model, **common,
                )
        except AgentError as exc:
            self._field_error("err-name", str(exc))
            return self._summary([str(exc)])

        self.notify(f"agent '{agent.name}' created — OPENAGENT.md updated", severity="information")
        # Return to the Agents list, which reloads and now shows the new agent.
        self.app.pop_screen()
        self.app.open_section("agents")  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ helpers

    def _input(self, wid: str) -> str:
        return self.query_one(f"#{wid}", Input).value.strip()

    def _value(self, wid: str):
        value = self.query_one(f"#{wid}", Select).value
        return None if value is Select.BLANK else value

    def _field_error(self, wid: str, message: str) -> None:
        self.query_one(f"#{wid}", Label).update(f"✗ {message}")

    def _clear_errors(self) -> None:
        for wid in ("err-name", "err-cli", "err-provider", "err-model"):
            self.query_one(f"#{wid}", Label).update("")
        self.query_one("#error-summary", Static).update("")

    def _summary(self, errors: list[str]) -> None:
        self.query_one("#error-summary", Static).update(
            "[b]Cannot create agent:[/b]\n" + "\n".join(f"  • {e}" for e in errors)
        )
