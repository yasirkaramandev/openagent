"""Doctor screen (spec §41)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static

from ..markup import safe_markup


class DoctorScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("r", "reload", "Refresh")]

    _MARKS = {"ok": "[green]✓[/green]", "warn": "[yellow]⚠[/yellow]", "fail": "[red]✗[/red]"}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Doctor", classes="screen-title")
        with VerticalScroll(id="doctor-body"):
            yield Static(id="checks", classes="panel")
        with Horizontal(classes="action-bar"):
            yield Button("Refresh", id="doctor-refresh")
            yield Button("Back", id="doctor-back")
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._load(), exclusive=True)

    def action_reload(self) -> None:
        self.run_worker(self._load(), exclusive=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "doctor-refresh":
            self.action_reload()
        elif event.button.id == "doctor-back":
            self.app.pop_screen()

    async def _load(self) -> None:
        checks = await self.app.oa.doctor.run()  # type: ignore[attr-defined]
        lines = []
        for c in checks:
            mark = self._MARKS.get(c.status, "?")
            detail = f"  [dim]{safe_markup(c.detail, 500)}[/dim]" if c.detail else ""
            lines.append(f"{mark} {safe_markup(c.name, 100)}{detail}")
        self.query_one("#checks", Static).update("\n".join(lines))
