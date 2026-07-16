"""Edit-agent screen — change an agent's mutable fields (spec §31).

Runtime and name are immutable (they define the agent); title, description, tags, permission
profile, and system prompt can be edited. Saving refreshes OPENAGENT.md via the service layer.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, Select, Static, TextArea

from ...core.permissions import profile_names
from ...services.agent_service import AgentError
from ..markup import safe_markup
from ..select_utils import selected_string


class EditAgentScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+s", "save", "Save"),
        Binding("f10", "save", "Save"),
    ]
    DEFAULT_CSS = """
    EditAgentScreen #form { height: 1fr; padding: 0 2; }
    EditAgentScreen Label { margin: 1 0 0 0; text-style: bold; }
    EditAgentScreen #system_prompt { height: 6; border: round $primary; }
    EditAgentScreen #action-bar { height: 3; padding: 0 2; background: $panel; }
    EditAgentScreen #action-bar Button { margin: 0 2 0 0; }
    EditAgentScreen #edit-error { color: $error; padding: 0 2; height: auto; }
    """

    def __init__(self, name: str) -> None:
        super().__init__()
        self.agent_name = name

    def compose(self) -> ComposeResult:
        agent = self.app.oa.agents.get(self.agent_name)  # type: ignore[attr-defined]
        profiles = [(p, p) for p in profile_names()]
        yield Header()
        yield Static(f"Edit Agent · {safe_markup(self.agent_name, 80)}", classes="screen-title")
        with VerticalScroll(id="form"):
            yield Label("Title")
            yield Input(value=agent.title if agent else "", id="title")
            yield Label("Description")
            yield Input(value=agent.description if agent else "", id="description")
            yield Label("Tags (comma-separated)")
            yield Input(value=", ".join(agent.tags) if agent else "", id="tags")
            yield Label("Permission profile")
            yield Select(
                profiles,
                value=agent.permission_profile if agent else "safe-edit",
                id="profile",
                allow_blank=False,
            )
            yield Label("System prompt (optional)")
            ta = TextArea(id="system_prompt")
            if agent:
                ta.text = agent.system_prompt
            yield ta
            yield Static("", id="edit-error")
        with Horizontal(id="action-bar", classes="action-bar"):
            yield Button("Save (Ctrl+S)", variant="success", id="save")
            yield Button("Cancel (Esc)", id="cancel")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        elif event.button.id == "cancel":
            self.app.pop_screen()

    def action_save(self) -> None:
        oa = self.app.oa  # type: ignore[attr-defined]
        tags = [t.strip() for t in self.query_one("#tags", Input).value.split(",") if t.strip()]
        try:
            oa.agents.update(
                self.agent_name,
                title=self.query_one("#title", Input).value.strip(),
                description=self.query_one("#description", Input).value.strip(),
                tags=tags,
                system_prompt=self.query_one("#system_prompt", TextArea).text.strip(),
                permission_profile=selected_string(self.query_one("#profile", Select))
                or "safe-edit",
            )
        except AgentError as exc:
            self.query_one("#edit-error", Static).update(f"[red]{safe_markup(str(exc), 300)}[/red]")
            return
        self.notify(f"agent '{self.agent_name}' updated — OPENAGENT.md refreshed")
        self.dismiss(True)
