"""OpenAgent Textual application (spec §31).

Bare ``openagent`` opens this. The dashboard shows counts and a menu; each menu entry pushes a
screen (Agents, Providers, Models, CLI Tools, Runs, Run Agent, Doctor). All data comes from the same
service layer the CLI uses.

**The app owns runs, not the screens** (item 10). A Textual worker belongs to the node that created
it, so a run started by a screen would be cancelled the moment that screen was popped — closing the
console would silently kill the agent. Runs are therefore executed as app-level workers and tracked
in :class:`LiveRun` objects that screens *subscribe* to. Leaving the console detaches a subscriber;
reopening it replays ``events.jsonl`` and re-subscribes. The same object is what lets Ctrl+C inside
an approval/question modal cancel the run underneath it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, ListItem, ListView, Static

from ..app import OpenAgentApp
from ..core.events import NormalizedEvent
from ..core.models import Run, enum_value
from ..core.projection import RunProjection
from ..security.approvals import ApprovalRequest
from .screens.add_agent import AddAgentScreen
from .screens.doctor import DoctorScreen
from .screens.lists import AgentsScreen, CliToolsScreen, ProvidersScreen, RunsScreen
from .screens.modals import ApprovalModal, QuestionModal
from .screens.run_console import RunSetupScreen

_MENU = [
    ("agents", "Agents", "Registered agents"),
    ("providers", "Providers", "API provider connections"),
    ("cli", "CLI Tools", "Installed coding CLIs"),
    ("runs", "Runs", "Recent runs and their output"),
    ("new_run", "Run Agent", "Run an agent on a task"),
    ("add_agent", "Add Agent", "Register a new agent"),
    ("doctor", "Doctor", "System diagnostics"),
]

#: Bound on the events one live run keeps in memory (the full log is always on disk).
MAX_LIVE_EVENTS = 5_000

EventHook = Callable[[NormalizedEvent], None]


class LiveRun:
    """A run this app is executing: its projected state, its event tail, and its subscribers."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.projection = RunProjection(run_id)
        self.events: list[NormalizedEvent] = []
        self.finished = False
        self.error: str | None = None
        self._subscribers: list[EventHook] = []

    def subscribe(self, hook: EventHook) -> None:
        if hook not in self._subscribers:
            self._subscribers.append(hook)

    def unsubscribe(self, hook: EventHook) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(hook)

    def publish(self, event: NormalizedEvent) -> None:
        """Fold an event into the projection and notify whoever is watching. Runs on the UI thread."""

        self.projection.apply(event)
        self.events.append(event)
        if len(self.events) > MAX_LIVE_EVENTS:
            del self.events[: len(self.events) - MAX_LIVE_EVENTS]
        for hook in list(self._subscribers):
            hook(event)

    def finish(self, error: str | None = None) -> None:
        self.finished = True
        self.error = error


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
        active = [r for r in runs if (enum_value(r.status))
                  in ("running", "starting", "queued", "waiting_approval")]
        failed = [r for r in runs if (enum_value(r.status)) == "failed"]
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
    """
    BINDINGS = [Binding("q", "quit", "Quit"), Binding("escape", "home", "Home")]

    def __init__(self, oa: OpenAgentApp | None = None) -> None:
        super().__init__()
        self.oa = oa or OpenAgentApp.create()
        #: Runs this app is executing right now, keyed by run id.
        self.live_runs: dict[str, LiveRun] = {}
        #: The approval/question modal a run is currently blocked on, so cancel can release it.
        self._open_modals: dict[str, object] = {}

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
            "new_run": RunSetupScreen,
            "add_agent": AddAgentScreen,
            "doctor": DoctorScreen,
        }
        screen_cls = screens.get(key)
        if screen_cls is not None:
            self.push_screen(screen_cls())

    # ------------------------------------------------------------------ running (app-owned)

    def live_run(self, run_id: str) -> LiveRun | None:
        return self.live_runs.get(run_id)

    def start_run(self, run: Run) -> LiveRun:
        """Execute ``run`` as an **app-level** worker so no screen owns (or can kill) it."""

        live = LiveRun(run.id)
        self.live_runs[run.id] = live
        self.run_worker(
            lambda: self._execute_run(run, live), thread=True, exclusive=False,
            name=f"run-{run.id}", group="runs",
        )
        return live

    def resume_run(self, run_id: str, prompt: str) -> LiveRun:
        """Send a follow-up turn into the same session (item 20)."""

        live = LiveRun(run_id)
        # Keep the earlier turns visible: replay first, then append this turn's events.
        live.projection = self.oa.runs.projection(run_id)
        self.live_runs[run_id] = live
        self.run_worker(
            lambda: self._execute_resume(run_id, prompt, live), thread=True, exclusive=False,
            name=f"resume-{run_id}", group="runs",
        )
        return live

    def _execute_run(self, run: Run, live: LiveRun) -> None:
        """Thread worker: drive the run and marshal every event back onto the UI thread."""

        def on_event(event: NormalizedEvent) -> None:
            self.call_from_thread(live.publish, event)

        def approval(request: ApprovalRequest) -> bool:
            return bool(self.call_from_thread(self._ask_approval, run.id, request))  # type: ignore[arg-type]

        def ask_user(question: str) -> str | None:
            return self.call_from_thread(self._ask_question, run.id, question)  # type: ignore[arg-type]

        error: str | None = None
        try:
            asyncio.run(self.oa.runs.execute(
                run, on_event=on_event, approval_callback=approval, ask_user_callback=ask_user,
            ))
        except Exception as exc:  # noqa: BLE001 - surfaced in the console, never a crash dialog
            error = str(exc)
        finally:
            self.call_from_thread(live.finish, error)
            self.call_from_thread(self._close_modal, run.id)

    def _execute_resume(self, run_id: str, prompt: str, live: LiveRun) -> None:
        def on_event(event: NormalizedEvent) -> None:
            self.call_from_thread(live.publish, event)

        error: str | None = None
        try:
            asyncio.run(self.oa.runs.resume(run_id, prompt, on_event=on_event))
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        finally:
            self.call_from_thread(live.finish, error)

    # ------------------------------------------------------------------ blocking prompts

    async def _ask_approval(self, run_id: str, request: ApprovalRequest) -> bool:
        modal = ApprovalModal(request)
        self._open_modals[run_id] = modal
        try:
            return bool(await self.push_screen_wait(modal))  # type: ignore[arg-type]
        finally:
            self._open_modals.pop(run_id, None)

    async def _ask_question(self, run_id: str, question: str) -> str | None:
        modal = QuestionModal(question, run_id=run_id)
        self._open_modals[run_id] = modal
        try:
            return await self.push_screen_wait(modal)
        finally:
            self._open_modals.pop(run_id, None)

    def _close_modal(self, run_id: str) -> None:
        modal = self._open_modals.pop(run_id, None)
        if modal is not None:
            with contextlib.suppress(Exception):
                modal.dismiss(None)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ cancellation

    def cancel_active_run(self, run_id: str) -> None:
        """Cancel a run from anywhere — including from inside the modal it is blocked on (item 9).

        Order matters. The cancellation flag is raised **first**, synchronously, so that when the
        modal is released the waiting worker finds the run already cancelled and stops at its next
        checkpoint. Releasing the modal first would let the tool return normally and the agent loop
        carry on to ``completed`` — which is exactly the bug this replaces.
        """

        self.oa.runs.cancellations.cancel(run_id)
        self._close_modal(run_id)
        self.run_worker(self._cancel(run_id), exclusive=False, group="cancel")

    async def _cancel(self, run_id: str) -> None:
        await self.oa.runs.cancel(run_id)


def run_tui() -> None:
    OpenAgentTUI().run()
