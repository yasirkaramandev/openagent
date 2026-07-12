"""New-run screen (live event stream, approval modal, cancel) and output viewer (spec §31)."""

from __future__ import annotations

import asyncio

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
    TextArea,
)

from ...core.events import NormalizedEvent
from ...security.approvals import ApprovalRequest
from ...services.run_service import RunError
from .modals import ApprovalModal


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
        "approval.requested": f"[yellow]⚠ approval requested: {d.get('command', '')}[/yellow]",
        "approval.accepted": "[green]✓ approved[/green]",
        "approval.denied": "[red]✗ denied[/red]",
        "file.created": f"[green]+ {d.get('path', '')}[/green]",
        "file.modified": f"[yellow]✎ {d.get('path', '')}[/yellow]",
        "file.deleted": f"[red]- {d.get('path', '')}[/red]",
        "test.completed": f"[magenta]tests {'passed' if d.get('passed') else 'failed'}[/magenta]",
        "usage.updated": f"[dim]tokens in={d.get('input_tokens')} out={d.get('output_tokens')}[/dim]",
        "run.completed": "[green]● completed[/green]",
        "run.cancelled": "[yellow]● cancelled[/yellow]",
        "run.failed": f"[red]● failed {d.get('message', '')}[/red]",
    }
    return table.get(etype, "")


class NewRunScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+c", "cancel_run", "Cancel run"),
    ]

    def __init__(self, preselect: str | None = None) -> None:
        super().__init__()
        self._preselect = preselect
        self._run_id: str | None = None

    def compose(self) -> ComposeResult:
        agents = [(a.name, a.name) for a in self.app.oa.agents.list()]  # type: ignore[attr-defined]
        yield Header()
        yield Static("New Run", classes="screen-title")
        agent_kwargs: dict = {}
        if self._preselect and any(value == self._preselect for _, value in agents):
            agent_kwargs["value"] = self._preselect
        with Horizontal():
            with Vertical(classes="panel"):
                yield Label("Agent")
                yield Select(agents, prompt="select agent", id="agent", allow_blank=True,
                             **agent_kwargs)
                yield Label("Prompt")
                yield Input(placeholder="describe the task", id="prompt")
                yield Label("Worktree")
                yield Select([("auto", "auto"), ("none", "none"), ("copy", "copy")],
                             value="auto", id="worktree", allow_blank=False)
                with Horizontal(id="run-actions"):
                    yield Button("Run", variant="primary", id="run")
                    yield Button("Cancel run", id="cancel-run")
            with Vertical(classes="panel"):
                yield Label("Activity")
                yield RichLog(id="run-log", markup=True, highlight=False, wrap=True)
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run":
            self._start_run()
        elif event.button.id == "cancel-run":
            self.action_cancel_run()

    def _start_run(self) -> None:
        agent = self._value("agent")
        prompt = self.query_one("#prompt", Input).value.strip()
        if not agent or not prompt:
            self.notify("choose an agent and enter a prompt", severity="warning")
            return
        oa = self.app.oa  # type: ignore[attr-defined]
        worktree = self._value("worktree") or "auto"
        try:
            run = oa.runs.create(agent_name=agent, prompt=prompt, worktree=worktree,
                                 confirm_in_place=(worktree == "none"))
        except RunError as exc:
            self.notify(str(exc), severity="error")
            return
        self._run_id = run.id
        log = self.query_one("#run-log", RichLog)
        log.write(f"[dim]run {run.id} starting… (worktree: {worktree})[/dim]")
        self.query_one("#run", Button).disabled = True
        # Execute in a thread worker so an approval modal can pause the run without blocking the UI.
        self.run_worker(lambda: self._execute_threaded(run), thread=True, exclusive=True)

    def _execute_threaded(self, run) -> None:
        def on_event(event: NormalizedEvent) -> None:
            line = format_event(event)
            if line:
                self.app.call_from_thread(self._write_log, line)

        def approval(request: ApprovalRequest) -> bool:
            # call_from_thread awaits the coroutine on the UI thread and returns its result.
            return bool(self.app.call_from_thread(self._ask_approval, request))  # type: ignore[arg-type]

        try:
            result = asyncio.run(
                self.app.oa.runs.execute(run, on_event=on_event, approval_callback=approval)  # type: ignore[attr-defined]
            )
            status = result.status if isinstance(result.status, str) else result.status.value
            self.app.call_from_thread(
                self._write_log,
                f"[b]done:[/b] {status} — output: openagent output --id {result.id}",
            )
        except Exception as exc:  # noqa: BLE001 - surface any runtime error in the log
            self.app.call_from_thread(self._write_log, f"[red]error: {exc}[/red]")
        finally:
            self.app.call_from_thread(self._finish)

    def _write_log(self, line: str) -> None:
        self.query_one("#run-log", RichLog).write(line)

    def _finish(self) -> None:
        self.query_one("#run", Button).disabled = False

    async def _ask_approval(self, request: ApprovalRequest) -> bool:
        """Push the approval modal on the UI thread and block the run worker until answered."""
        done = asyncio.Event()
        holder = {"approved": False}

        def resolved(value: bool | None) -> None:
            holder["approved"] = bool(value)
            done.set()

        self.app.push_screen(ApprovalModal(request), resolved)
        await done.wait()
        return holder["approved"]

    def action_cancel_run(self) -> None:
        if not self._run_id:
            return
        self.run_worker(self._cancel(self._run_id), exclusive=False)

    async def _cancel(self, run_id: str) -> None:
        await self.app.oa.runs.cancel(run_id)  # type: ignore[attr-defined]
        self._write_log(f"[yellow]cancel requested for {run_id}[/yellow]")

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
                    # Read-only TextArea: holds and scrolls its content regardless of which tab is
                    # active (a RichLog rendered while hidden would drop its lines).
                    yield TextArea(id=f"log-{fmt}", read_only=True, soft_wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        oa = self.app.oa  # type: ignore[attr-defined]
        for fmt in ("md", "diff", "logs", "json", "events"):
            widget = self.query_one(f"#log-{fmt}", TextArea)
            try:
                widget.text = oa.runs.output(self.run_id, fmt)
            except RunError as exc:
                widget.text = f"(no {fmt} artifact: {exc})"
