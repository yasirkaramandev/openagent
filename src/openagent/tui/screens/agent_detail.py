"""Agent detail screen — full view of one agent (spec §31)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Static


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
            f"Agent · {self.agent_name}  ([b]R[/b] run · [b]E[/b] edit)", classes="screen-title"
        )
        with VerticalScroll():
            yield Static(self._body_text(), id="body")
        yield Footer()

    def _body_text(self) -> str:
        agent = self.app.oa.agents.get(self.agent_name)  # type: ignore[attr-defined]
        if not agent:
            return "[red]agent not found[/red]"
        rt = agent.runtime
        rtype = rt.type if isinstance(rt.type, str) else rt.type.value
        binding = (
            f"- CLI: `{rt.cli}`"
            if rtype == "cli"
            else f"- Provider: `{rt.provider}`\n- Model: `{rt.model}`"
        )
        return (
            f"[b]{agent.title or agent.name}[/b]\n\n"
            f"- Name: `{agent.name}`\n"
            f"- Runtime: `{rtype}`\n"
            f"{binding}\n"
            f"- Permission profile: `{agent.permission_profile}`\n"
            f"- Tags: {', '.join(agent.tags) or '—'}\n"
            f"- Max steps: {agent.max_steps}\n\n"
            f"[b]Description[/b]\n{agent.description or '—'}\n\n"
            f"[b]System prompt[/b]\n{agent.system_prompt or '—'}\n"
        )

    def action_run(self) -> None:
        from .run_console import RunSetupScreen

        self.app.push_screen(RunSetupScreen(preselect=self.agent_name))

    def action_edit(self) -> None:
        from .edit_agent import EditAgentScreen

        def refresh(_: object = None) -> None:
            self.query_one("#body", Static).update(self._body_text())

        self.app.push_screen(EditAgentScreen(self.agent_name), callback=refresh)
