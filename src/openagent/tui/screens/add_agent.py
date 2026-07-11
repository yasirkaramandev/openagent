"""Add-agent wizard (spec §31 add-agent flow).

A form: pick a runtime (API or CLI), fill in identity + binding + permission profile, create. On
success the agent is saved and OPENAGENT.md is refreshed by the service layer.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, Select, Static

from ...core.models import RuntimeType
from ...core.permissions import profile_names
from ...runtimes.cli.registry import known_cli_types
from ...services.agent_service import AgentError


class AddAgentScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        oa = self.app.oa  # type: ignore[attr-defined]
        providers = [(p.name, p.name) for p in oa.providers.list()]
        clis = [(t, t) for t in known_cli_types()]
        profiles = [(p, p) for p in profile_names()]
        yield Header()
        yield Static("Add Agent", classes="screen-title")
        with Vertical(classes="panel"):
            yield Label("Runtime")
            yield Select([("API agent", "api"), ("CLI agent", "cli")], value="api", id="runtime")
            yield Label("Name")
            yield Input(placeholder="e.g. deepseek-coder", id="name")
            yield Label("Title")
            yield Input(placeholder="DeepSeek Backend Coder", id="title")
            yield Label("Description")
            yield Input(placeholder="what this agent is for", id="description")
            yield Label("Provider (API)")
            yield Select(providers, prompt="select provider", id="provider",
                         allow_blank=True)
            yield Label("Model id (API)")
            yield Input(placeholder="remote model id", id="model")
            yield Label("CLI (CLI agent)")
            yield Select(clis, prompt="select CLI", id="cli", allow_blank=True)
            yield Label("Tags (comma-separated)")
            yield Input(placeholder="coder, python", id="tags")
            yield Label("Permission profile")
            yield Select(profiles, value="safe-edit", id="profile")
            yield Button("Create agent", variant="primary", id="create")
            yield Static("", id="result")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "create":
            return
        oa = self.app.oa  # type: ignore[attr-defined]
        runtime = self._value("runtime")
        name = self._input("name")
        if not name:
            self._error("name is required")
            return
        tags = [t.strip() for t in self._input("tags").split(",") if t.strip()]
        profile = self._value("profile") or "safe-edit"
        try:
            if runtime == "cli":
                cli = self._value("cli")
                if not cli:
                    self._error("choose a CLI")
                    return
                oa.agents.create(
                    name=name, title=self._input("title"), description=self._input("description"),
                    runtime_type=RuntimeType.CLI, cli=cli, tags=tags, permission_profile=profile,
                )
            else:
                provider = self._value("provider")
                model = self._input("model")
                if not provider or not model:
                    self._error("API agents need a provider and a model id")
                    return
                oa.agents.create(
                    name=name, title=self._input("title"), description=self._input("description"),
                    runtime_type=RuntimeType.API_AGENT, provider=provider, model=model,
                    tags=tags, permission_profile=profile,
                )
        except AgentError as exc:
            self._error(str(exc))
            return
        self.notify(f"agent '{name}' created; OPENAGENT.md updated")
        self.app.pop_screen()

    def _input(self, wid: str) -> str:
        return self.query_one(f"#{wid}", Input).value.strip()

    def _value(self, wid: str):
        value = self.query_one(f"#{wid}", Select).value
        return None if value is Select.BLANK else value

    def _error(self, message: str) -> None:
        self.query_one("#result", Static).update(f"[red]{message}[/red]")
