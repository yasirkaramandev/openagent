"""New-run screen (live event stream) and output viewer (spec §31)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from ...core.events import NormalizedEvent
from ...services.run_service import RunError


def format_event(event: NormalizedEvent) -> str:
    etype = event.type if isinstance(event.type, str) else event.type.value
    d = event.data
    table = {
        "run.started": "[dim]▶ run started[/dim]",
        "session.created": f"[dim]session {d.get('provider_session_id', '')}[/dim]",
        "message.completed": f"[white]{(d.get('text') or '').strip()[:120]}[/white]",
        "tool.requested": f"[cyan]→ {d.get('tool', '')}[/cyan]",
        "tool.completed": f"[green]✓ {d.get('tool', '')}[/green]",
        "tool.failed": f"[red]✗ {d.get('tool', '')}[/red]",
        "command.started": f"[blue]$ {d.get('command', '')}[/blue]",
        "command.completed": f"[blue]$ exit {d.get('exit_code')}[/blue]",
        "file.created": f"[green]+ {d.get('path', '')}[/green]",
        "file.modified": f"[yellow]✎ {d.get('path', '')}[/yellow]",
        "file.deleted": f"[red]- {d.get('path', '')}[/red]",
        "test.completed": f"[magenta]tests {'passed' if d.get('passed') else 'failed'}[/magenta]",
        "usage.updated": f"[dim]tokens in={d.get('input_tokens')} out={d.get('output_tokens')}[/dim]",
        "run.completed": "[green]● completed[/green]",
        "run.failed": f"[red]● failed {d.get('message', '')}[/red]",
    }
    return table.get(etype, "")


class NewRunScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        agents = [(a.name, a.name) for a in self.app.oa.agents.list()]  # type: ignore[attr-defined]
        yield Header()
        yield Static("New Run", classes="screen-title")
        with Horizontal():
            with Vertical(classes="panel"):
                yield Label("Agent")
                yield Select(agents, prompt="select agent", id="agent", allow_blank=True)
                yield Label("Prompt")
                yield Input(placeholder="describe the task", id="prompt")
                yield Label("Worktree")
                yield Select([("auto", "auto"), ("none", "none")], value="auto", id="worktree")
                yield Button("Run", variant="primary", id="run")
            with Vertical(classes="panel"):
                yield Label("Activity")
                yield RichLog(id="run-log", markup=True, highlight=False, wrap=True)
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "run":
            return
        agent = self._value("agent")
        prompt = self.query_one("#prompt", Input).value.strip()
        if not agent or not prompt:
            self.notify("choose an agent and enter a prompt", severity="warning")
            return
        oa = self.app.oa  # type: ignore[attr-defined]
        try:
            run = oa.runs.create(agent_name=agent, prompt=prompt,
                                 worktree=self._value("worktree") or "auto")
        except RunError as exc:
            self.notify(str(exc), severity="error")
            return
        log = self.query_one("#run-log", RichLog)
        log.write(f"[dim]run {run.id} starting…[/dim]")
        self.query_one("#run", Button).disabled = True
        self.run_worker(self._execute(run), exclusive=True)

    async def _execute(self, run) -> None:
        log = self.query_one("#run-log", RichLog)

        def on_event(event: NormalizedEvent) -> None:
            line = format_event(event)
            if line:
                log.write(line)

        try:
            result = await self.app.oa.runs.execute(run, on_event=on_event)  # type: ignore[attr-defined]
            status = result.status if isinstance(result.status, str) else result.status.value
            log.write(f"[b]done:[/b] {status} — output: openagent output --id {result.id}")
        except Exception as exc:  # noqa: BLE001 - surface any runtime error in the log
            log.write(f"[red]error: {exc}[/red]")
        finally:
            self.query_one("#run", Button).disabled = False

    def _value(self, wid: str):
        value = self.query_one(f"#{wid}", Select).value
        return None if value is Select.BLANK else value


class OutputScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"Run {self.run_id}", classes="screen-title")
        with TabbedContent():
            for label, fmt in (("Output", "md"), ("Diff", "diff"), ("Logs", "logs"),
                               ("Result", "json"), ("Events", "events")):
                with TabPane(label, id=f"tab-{fmt}"):
                    yield RichLog(id=f"log-{fmt}", markup=False, highlight=False, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        oa = self.app.oa  # type: ignore[attr-defined]
        for fmt in ("md", "diff", "logs", "json", "events"):
            widget = self.query_one(f"#log-{fmt}", RichLog)
            try:
                widget.write(oa.runs.output(self.run_id, fmt))
            except RunError as exc:
                widget.write(f"(no {fmt} artifact: {exc})")
