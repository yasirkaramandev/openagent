"""DB-independent Textual recovery surface for startup and migration failures."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static

from .. import __version__
from ..core.errors import redact_secrets
from ..security.process import minimal_environment, run_capture
from .markup import safe_markup


@dataclass(frozen=True)
class RecoveryDetails:
    category: str
    description: str
    binary_path: str
    binary_version: str
    required_minimum: str
    schema_version: str
    backup: str
    repair_command: str


def recovery_details(error: Exception) -> RecoveryDetails:
    category = getattr(getattr(error, "error_type", None), "value", None) or type(error).__name__
    binary_path = str(getattr(error, "binary_path", None) or sys.executable)
    binary_version = str(getattr(error, "binary_version", None) or __version__)
    required = str(getattr(error, "minimum_reader_version", None) or "not available")
    schema = str(getattr(error, "database_schema", None) or "not available")
    backup_path = getattr(error, "backup_path", None)
    commands = getattr(error, "repair_commands", None) or ["openagent update --repair"]
    description = redact_secrets(str(error))
    return RecoveryDetails(
        category=str(category),
        description=description,
        binary_path=binary_path,
        binary_version=binary_version,
        required_minimum=required,
        schema_version=schema,
        backup="available" if backup_path else "not reported",
        repair_command=str(commands[0]),
    )


class RecoveryScreen(Screen):
    BINDINGS = [Binding("escape", "app.quit", "Quit")]

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.details = recovery_details(error)

    def compose(self) -> ComposeResult:
        d = self.details
        yield Header()
        yield Static("Database recovery", classes="screen-title")
        with VerticalScroll(id="recovery-body"):
            yield Static(
                "\n".join(
                    (
                        f"[b]Category:[/b] {safe_markup(d.category, 100)}",
                        f"[b]Description:[/b] {safe_markup(d.description, 1200)}",
                        f"[b]Active binary:[/b] {safe_markup(d.binary_path, 400)}",
                        f"[b]Binary version:[/b] {safe_markup(d.binary_version, 100)}",
                        f"[b]Required minimum:[/b] {safe_markup(d.required_minimum, 100)}",
                        f"[b]Schema version:[/b] {safe_markup(d.schema_version, 100)}",
                        f"[b]Backup:[/b] {safe_markup(d.backup, 100)}",
                        f"[b]Safe repair:[/b] {safe_markup(d.repair_command, 300)}",
                    )
                ),
                id="recovery-details",
                classes="panel",
            )
            yield Static("", id="recovery-output", classes="panel")
        with Horizontal(classes="action-bar"):
            yield Button("Version", id="recovery-version")
            yield Button("Doctor", id="recovery-doctor")
            yield Button("Update", id="recovery-update")
            yield Button("Repair", id="recovery-repair", variant="warning")
            yield Button("Quit", id="recovery-quit", variant="error")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        commands = {
            "recovery-version": ["version"],
            "recovery-doctor": ["doctor", "--json"],
            "recovery-update": ["update"],
            "recovery-repair": ["update", "--repair"],
        }
        if event.button.id == "recovery-quit":
            self.app.exit()
            return
        command = commands.get(event.button.id or "")
        if command is not None:
            self.run_worker(self._run_command(command), exclusive=True)

    async def _run_command(self, command: list[str]) -> None:
        output = self.query_one("#recovery-output", Static)
        output.update("Running safe recovery command…")
        try:
            result = await asyncio.to_thread(
                run_capture,
                [sys.executable, "-m", "openagent", *command],
                cwd=Path.cwd(),
                env=minimal_environment(),
                timeout=180,
                shell=False,
                max_output_bytes=1024 * 1024,
            )
            text = redact_secrets((result.stdout or result.stderr or "no output")[:4000])
            output.update(safe_markup(text, 4000))
        except Exception as exc:  # noqa: BLE001 - recovery must remain usable
            output.update(safe_markup(redact_secrets(str(exc)), 1000))


class DatabaseRecoveryTUI(App):
    """A minimal app that deliberately has no OpenAgentApp/Database dependency."""

    TITLE = "OpenAgent — Database recovery"
    CSS = """
    #recovery-body { height: 1fr; }
    .panel { border: round $primary; padding: 1 2; margin: 1; }
    .screen-title { padding: 0 1; text-style: bold; color: $accent; }
    .action-bar { height: 3; min-height: 3; padding: 0 1; background: $panel; }
    .action-bar Button { width: 1fr; min-width: 0; margin: 0; }
    """

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def on_mount(self) -> None:
        self.push_screen(RecoveryScreen(self.error))
