"""Agent detail screen — full view of one agent (spec §31)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static

from ..markup import safe_markup


class AgentDetailScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("r", "run", "Run"),
        Binding("e", "edit", "Edit"),
    ]

    def __init__(self, name: str) -> None:
        super().__init__()
        self.agent_name = name

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            f"Agent · {safe_markup(self.agent_name, 80)}  ([b]R[/b] run · [b]E[/b] edit)",
            classes="screen-title",
        )
        with VerticalScroll():
            yield Static(self._body_text(), id="body")
        with Horizontal(classes="action-bar"):
            yield Button("Run", id="detail-run")
            yield Button("Edit", id="detail-edit")
            yield Button("Back", id="detail-back")
        yield Footer()

    def _body_text(self) -> str:
        agent = self.app.oa.agents.get(self.agent_name)  # type: ignore[attr-defined]
        if not agent:
            return "[red]agent not found[/red]"
        rt = agent.runtime
        rtype = rt.type if isinstance(rt.type, str) else rt.type.value
        binding = (
            f"- CLI: `{safe_markup(rt.cli, 80)}`"
            if rtype == "cli"
            else f"- Provider: `{safe_markup(rt.provider, 80)}`\n"
            f"- Model: `{safe_markup(rt.model, 120)}`"
        )
        return (
            f"[b]{safe_markup(agent.title or agent.name, 120)}[/b]\n\n"
            f"- Name: `{safe_markup(agent.name, 80)}`\n"
            f"- Runtime: `{rtype}`\n"
            f"{binding}\n"
            f"- Permission profile: `{safe_markup(agent.permission_profile, 40)}`\n"
            f"- Tags: {safe_markup(', '.join(agent.tags), 200) or '—'}\n"
            f"- Max steps: {agent.max_steps}\n\n"
            f"[b]Description[/b]\n{safe_markup(agent.description, 2000) or '—'}\n\n"
            f"[b]System prompt[/b]\n{safe_markup(agent.system_prompt, 4000) or '—'}\n"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "detail-run":
            self.action_run()
        elif event.button.id == "detail-edit":
            self.action_edit()
        elif event.button.id == "detail-back":
            self.app.pop_screen()

    def action_run(self) -> None:
        from .run_console import RunSetupScreen

        self.app.push_screen(RunSetupScreen(preselect=self.agent_name))

    def action_edit(self) -> None:
        from .edit_agent import EditAgentScreen

        def refresh(_: object = None) -> None:
            self.query_one("#body", Static).update(self._body_text())

        self.app.push_screen(EditAgentScreen(self.agent_name), callback=refresh)
