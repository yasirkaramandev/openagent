"""Doctor screen (spec §41)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, Static


class DoctorScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("r", "reload", "Refresh")]

    _MARKS = {"ok": "[green]✓[/green]", "warn": "[yellow]⚠[/yellow]", "fail": "[red]✗[/red]"}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Doctor", classes="screen-title")
        yield Static(id="checks", classes="panel")
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._load(), exclusive=True)

    def action_reload(self) -> None:
        self.run_worker(self._load(), exclusive=True)

    async def _load(self) -> None:
        checks = await self.app.oa.doctor.run()  # type: ignore[attr-defined]
        lines = []
        for c in checks:
            mark = self._MARKS.get(c.status, "?")
            detail = f"  [dim]{c.detail}[/dim]" if c.detail else ""
            lines.append(f"{mark} {c.name}{detail}")
        self.query_one("#checks", Static).update("\n".join(lines))
