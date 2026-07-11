"""OpenAgent Textual application (spec §31).

Bare ``openagent`` opens this. The dashboard shows counts and a menu; each menu entry pushes a
screen (Agents, Providers, Models, CLI Tools, Runs, New Run, Doctor). All data comes from the same
service layer the CLI uses.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, ListItem, ListView, Static

from ..app import OpenAgentApp
from .screens.add_agent import AddAgentScreen
from .screens.doctor import DoctorScreen
from .screens.lists import AgentsScreen, CliToolsScreen, ProvidersScreen, RunsScreen
from .screens.run_view import NewRunScreen

_MENU = [
    ("agents", "Agents", "Registered agents"),
    ("providers", "Providers", "API provider connections"),
    ("cli", "CLI Tools", "Installed coding CLIs"),
    ("runs", "Runs", "Recent runs and their output"),
    ("new_run", "New Run", "Run an agent on a task"),
    ("add_agent", "Add Agent", "Register a new agent"),
    ("doctor", "Doctor", "System diagnostics"),
]


class DashboardScreen(Screen):
    """Home screen: stats + navigation menu (spec §31)."""

    BINDINGS = [Binding("r", "refresh", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="dash-body"):
            yield Static(id="stats", classes="panel")
            with Vertical(classes="panel"):
                yield Static("[b]Menu[/b]  (↑/↓ + Enter)", id="menu-title")
                yield ListView(
                    *[ListItem(Static(f"{label} — [dim]{desc}[/dim]"), id=f"m-{key}")
                      for key, label, desc in _MENU],
                    id="menu",
                )
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_stats()

    def action_refresh(self) -> None:
        self.refresh_stats()

    def refresh_stats(self) -> None:
        oa: OpenAgentApp = self.app.oa  # type: ignore[attr-defined]
        agents = oa.agents.list()
        providers = oa.providers.list()
        clis = oa.clis.list()
        runs = oa.runs.list(100)
        active = [r for r in runs if (r.status if isinstance(r.status, str) else r.status.value)
                  in ("running", "starting", "queued", "waiting_approval")]
        failed = [r for r in runs if (r.status if isinstance(r.status, str) else r.status.value) == "failed"]
        text = (
            f"[b]OpenAgent[/b]   project: [cyan]{oa.paths.project_root.name}[/cyan]\n\n"
            f"Agents         [b]{len(agents):>3}[/b]\n"
            f"Providers      [b]{len(providers):>3}[/b]\n"
            f"CLI Tools      [b]{len(clis):>3}[/b]\n"
            f"Runs (recent)  [b]{len(runs):>3}[/b]\n"
            f"Active runs    [b]{len(active):>3}[/b]\n"
            f"Failed runs    [b]{len(failed):>3}[/b]\n"
        )
        self.query_one("#stats", Static).update(text)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        key = (event.item.id or "").removeprefix("m-")
        self.app.open_section(key)  # type: ignore[attr-defined]


class OpenAgentTUI(App):
    """The OpenAgent terminal UI."""

    TITLE = "OpenAgent"
    SUB_TITLE = "control plane for AI APIs, coding CLIs, and agents"
    CSS = """
    #dash-body { height: 1fr; }
    .panel { border: round $primary; padding: 1 2; margin: 1; }
    #stats { width: 40%; }
    #menu { height: auto; }
    DataTable { height: 1fr; }
    .screen-title { padding: 0 1; text-style: bold; color: $accent; }
    #run-log { height: 1fr; border: round $primary; }
    """
    BINDINGS = [Binding("q", "quit", "Quit"), Binding("escape", "home", "Home")]

    def __init__(self, oa: OpenAgentApp | None = None) -> None:
        super().__init__()
        self.oa = oa or OpenAgentApp.create()

    def on_mount(self) -> None:
        self.oa.runs.recover_orphans()
        self.push_screen(DashboardScreen())

    def action_home(self) -> None:
        if len(self.screen_stack) > 2:
            self.pop_screen()

    def open_section(self, key: str) -> None:
        screens = {
            "agents": AgentsScreen,
            "providers": ProvidersScreen,
            "cli": CliToolsScreen,
            "runs": RunsScreen,
            "new_run": NewRunScreen,
            "add_agent": AddAgentScreen,
            "doctor": DoctorScreen,
        }
        screen_cls = screens.get(key)
        if screen_cls is not None:
            self.push_screen(screen_cls())


def run_tui() -> None:
    OpenAgentTUI().run()
