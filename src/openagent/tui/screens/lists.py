"""Table screens for Agents, Providers, CLI tools, and Runs (spec §31)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, Static

from ...core.models import AgentProfile
from ...services.provider_service import ProviderInUseError


def _runtime_label(a: AgentProfile) -> str:
    rt = a.runtime
    rtype = rt.type if isinstance(rt.type, str) else rt.type.value
    return "cli" if rtype == "cli" else "api"


def _provider_or_cli(a: AgentProfile) -> str:
    rt = a.runtime
    rtype = rt.type if isinstance(rt.type, str) else rt.type.value
    return (rt.cli or "—") if rtype == "cli" else (rt.provider or "—")


class _TableScreen(Screen):
    """Base: a titled DataTable with back/refresh bindings."""

    title_text = "Screen"
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self.title_text, classes="screen-title")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.reload()

    def action_refresh(self) -> None:
        self.reload()

    def reload(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        self.populate(table)

    def populate(self, table: DataTable) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class AgentsScreen(Screen):
    """Agents: a table plus a details panel, with run/edit/delete/add/search actions (spec §31)."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("enter", "details", "Details"),
        Binding("r", "run", "Run"),
        Binding("e", "edit", "Edit"),
        Binding("delete", "remove", "Delete"),
        Binding("a", "add", "Add"),
        Binding("slash", "search", "Search"),
        Binding("ctrl+r", "refresh", "Refresh"),
    ]
    DEFAULT_CSS = """
    AgentsScreen #agents-body { height: 1fr; }
    AgentsScreen #table { width: 2fr; height: 1fr; }
    AgentsScreen #details { width: 1fr; height: 1fr; border: round $primary; padding: 0 1; }
    AgentsScreen #search { display: none; }
    AgentsScreen #search.visible { display: block; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._filter = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Agents  ([b]Enter[/b] details · [b]R[/b] run · [b]E[/b] edit · "
                     "[b]Del[/b] remove · [b]A[/b] add · [b]/[/b] search)", classes="screen-title")
        yield Input(placeholder="filter by name/title/tag…  (Enter to apply, Esc to clear)", id="search")
        with Horizontal(id="agents-body"):
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            yield Static("", id="details")
        yield Footer()

    def on_mount(self) -> None:
        self.reload()

    def action_refresh(self) -> None:
        self.reload()

    def _agents(self) -> list[AgentProfile]:
        agents = list(self.app.oa.agents.list())  # type: ignore[attr-defined]
        if self._filter:
            f = self._filter.lower()
            agents = [a for a in agents
                      if f in a.name.lower() or f in (a.title or "").lower()
                      or any(f in t.lower() for t in a.tags)]
        return agents

    def reload(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_columns("Name", "Title", "Runtime", "Provider/CLI", "Model", "Tags", "Profile")
        for a in self._agents():
            model = a.runtime.model or "—" if _runtime_label(a) == "api" else "—"
            table.add_row(a.name, a.title or "—", _runtime_label(a), _provider_or_cli(a),
                          model, ", ".join(a.tags) or "—", a.permission_profile, key=a.name)
        self._update_details()

    def _selected_name(self) -> str | None:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0:
            return None
        try:
            return str(table.get_row_at(table.cursor_row)[0])
        except Exception:  # pragma: no cover - empty/transient
            return None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._update_details()

    def _update_details(self) -> None:
        name = self._selected_name()
        panel = self.query_one("#details", Static)
        agent = self.app.oa.agents.get(name) if name else None  # type: ignore[attr-defined]
        if not agent:
            panel.update("[dim]no agent selected[/dim]")
            return
        rt = agent.runtime
        binding = (f"CLI: {rt.cli}" if _runtime_label(agent) == "cli"
                   else f"Provider: {rt.provider}\nModel: {rt.model}")
        panel.update(
            f"[b]{agent.title or agent.name}[/b]\n"
            f"[dim]{agent.name}[/dim]\n\n"
            f"Runtime: {_runtime_label(agent)}\n{binding}\n"
            f"Profile: {agent.permission_profile}\n"
            f"Tags: {', '.join(agent.tags) or '—'}\n\n"
            f"[b]Description[/b]\n{agent.description or '—'}\n\n"
            f"[b]System prompt[/b]\n{(agent.system_prompt or '—')[:400]}"
        )

    # ------------------------------------------------------------------ actions

    def action_details(self) -> None:
        name = self._selected_name()
        if name:
            from .agent_detail import AgentDetailScreen
            self.app.push_screen(AgentDetailScreen(name))

    def action_run(self) -> None:
        name = self._selected_name()
        if name:
            from .run_view import NewRunScreen
            self.app.push_screen(NewRunScreen(preselect=name))

    def action_edit(self) -> None:
        name = self._selected_name()
        if name:
            from .edit_agent import EditAgentScreen
            self.app.push_screen(EditAgentScreen(name), callback=lambda _=None: self.reload())

    def action_remove(self) -> None:
        name = self._selected_name()
        if not name:
            return
        from .modals import ConfirmModal

        def done(confirmed: bool | None) -> None:
            if confirmed:
                self.app.oa.agents.remove(name)  # type: ignore[attr-defined]
                self.notify(f"removed agent '{name}' — OPENAGENT.md updated")
                self.reload()

        self.app.push_screen(
            ConfirmModal(f"Delete agent [b]{name}[/b]? This also updates OPENAGENT.md.",
                         confirm_label="Delete"),
            callback=done,
        )

    def action_add(self) -> None:
        from .add_agent import AddAgentScreen
        self.app.push_screen(AddAgentScreen())

    def action_search(self) -> None:
        search = self.query_one("#search", Input)
        search.add_class("visible")
        search.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            self._filter = event.value.strip()
            self.reload()
            self.query_one("#table", DataTable).focus()

    def on_key(self, event) -> None:
        # Esc while the search box is focused clears the filter instead of leaving the screen.
        if event.key == "escape" and self.focused and self.focused.id == "search":
            event.stop()
            search = self.query_one("#search", Input)
            search.value = ""
            search.remove_class("visible")
            self._filter = ""
            self.reload()
            self.query_one("#table", DataTable).focus()


class ProvidersScreen(_TableScreen):
    title_text = "Providers  ([b]A[/b] add · [b]D[/b] remove · [b]T[/b] test)"
    BINDINGS = _TableScreen.BINDINGS + [
        Binding("a", "add", "Add provider"),
        Binding("d", "remove", "Remove"),
        Binding("delete", "remove", "Remove"),
        Binding("t", "test", "Test connection"),
    ]

    def populate(self, table: DataTable) -> None:
        table.add_columns("Name", "Type", "Protocol", "Base URL", "Credential")
        for p in self.app.oa.providers.list():  # type: ignore[attr-defined]
            cred = p.credential.type if isinstance(p.credential.type, str) else p.credential.type.value
            table.add_row(p.name, p.provider_type, p.protocol.value, p.base_url or "(preset)", cred,
                          key=p.name)

    def action_add(self) -> None:
        from .add_provider import AddProviderScreen
        self.app.push_screen(AddProviderScreen(), callback=lambda _=None: self.reload())

    def action_remove(self) -> None:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0:
            return
        name = str(table.get_row_at(table.cursor_row)[0])
        from .modals import ConfirmModal

        def done(confirmed: bool | None) -> None:
            if confirmed:
                try:
                    self.app.oa.providers.remove(name)  # type: ignore[attr-defined]
                except ProviderInUseError as exc:
                    self.notify(str(exc), severity="error", timeout=8)
                    return
                self.notify(f"removed provider '{name}'")
                self.reload()

        self.app.push_screen(ConfirmModal(f"Remove provider [b]{name}[/b]?",
                                          confirm_label="Remove"), callback=done)

    def action_test(self) -> None:
        """Test the selected provider's connection and report the result (item 15)."""

        table = self.query_one("#table", DataTable)
        if table.row_count == 0:
            return
        name = str(table.get_row_at(table.cursor_row)[0])
        self.notify(f"testing '{name}'…")
        self.run_worker(self._test_provider(name), exclusive=True)

    async def _test_provider(self, name: str) -> None:
        try:
            result = await self.app.oa.providers.test(name)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - surface any failure as an unhealthy result
            self.notify(f"✗ {name}: {exc}", severity="error", timeout=8)
            return
        if result.ok:
            self.notify(f"✓ {name}: {result.detail}", severity="information", timeout=6)
        else:
            self.notify(f"✗ {name}: {result.detail}", severity="error", timeout=8)


class CliToolsScreen(_TableScreen):
    title_text = "CLI Tools"

    def on_mount(self) -> None:
        import asyncio

        try:
            asyncio.get_event_loop().create_task(self._discover())
        except RuntimeError:  # pragma: no cover
            pass
        self.reload()

    async def _discover(self) -> None:
        await self.app.oa.clis.discover(persist=True)  # type: ignore[attr-defined]
        self.reload()

    def populate(self, table: DataTable) -> None:
        table.add_columns("Type", "Version", "Executable", "Auth", "Adapter")
        for c in self.app.oa.clis.list():  # type: ignore[attr-defined]
            auth = "yes" if c.authenticated else ("no" if c.authenticated is False else "?")
            label = f"{c.type}{' (exp)' if c.experimental else ''}"
            table.add_row(label, c.version or "—", c.executable, auth, c.adapter)


class RunsScreen(_TableScreen):
    title_text = "Runs  ([b]Enter[/b] output · [b]C[/b] cancel)"
    BINDINGS = _TableScreen.BINDINGS + [
        Binding("enter", "open_output", "Output"),
        Binding("c", "cancel_run", "Cancel"),
    ]

    def populate(self, table: DataTable) -> None:
        table.add_columns("ID", "Agent", "Status", "Turns", "Started", "Files")
        for r in self.app.oa.runs.list(50):  # type: ignore[attr-defined]
            status = r.status if isinstance(r.status, str) else r.status.value
            table.add_row(r.id, r.agent, status, str(r.turns),
                          r.started_at.strftime("%m-%d %H:%M"), str(len(r.files_changed)), key=r.id)

    def action_open_output(self) -> None:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0:
            return
        run_id = str(table.get_row_at(table.cursor_row)[0])
        from .run_view import OutputScreen
        self.app.push_screen(OutputScreen(run_id))

    def action_cancel_run(self) -> None:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0:
            return
        run_id = str(table.get_row_at(table.cursor_row)[0])
        self.run_worker(self._cancel(run_id), exclusive=False)

    async def _cancel(self, run_id: str) -> None:
        await self.app.oa.runs.cancel(run_id)  # type: ignore[attr-defined]
        self.notify(f"cancelled {run_id}")
        self.reload()
