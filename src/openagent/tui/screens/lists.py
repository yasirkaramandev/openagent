"""Table screens for Agents, Providers, CLI tools, and Runs (spec §31)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static


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


class AgentsScreen(_TableScreen):
    title_text = "Agents"

    def populate(self, table: DataTable) -> None:
        table.add_columns("Name", "Title", "Runtime", "Tags", "Profile")
        for a in self.app.oa.agents.list():  # type: ignore[attr-defined]
            rt = a.runtime
            rtype = rt.type if isinstance(rt.type, str) else rt.type.value
            runtime = f"{rt.cli}-cli" if rtype == "cli" else f"api:{rt.provider}"
            table.add_row(a.name, a.title or "—", runtime, ", ".join(a.tags) or "—",
                          a.permission_profile)


class ProvidersScreen(_TableScreen):
    title_text = "Providers"

    def populate(self, table: DataTable) -> None:
        table.add_columns("Name", "Type", "Protocol", "Base URL", "Credential")
        for p in self.app.oa.providers.list():  # type: ignore[attr-defined]
            cred = p.credential.type if isinstance(p.credential.type, str) else p.credential.type.value
            table.add_row(p.name, p.provider_type, p.protocol.value, p.base_url or "(preset)", cred)


class CliToolsScreen(_TableScreen):
    title_text = "CLI Tools"

    def on_mount(self) -> None:
        # Discover synchronously via the service (fast, local).
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
    title_text = "Runs"
    BINDINGS = _TableScreen.BINDINGS + [Binding("enter", "open_output", "Output")]

    def populate(self, table: DataTable) -> None:
        table.add_columns("ID", "Agent", "Status", "Started", "Files")
        for r in self.app.oa.runs.list(50):  # type: ignore[attr-defined]
            status = r.status if isinstance(r.status, str) else r.status.value
            table.add_row(r.id, r.agent, status, r.started_at.strftime("%m-%d %H:%M"),
                          str(len(r.files_changed)))

    def action_open_output(self) -> None:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0:
            return
        run_id = table.get_row_at(table.cursor_row)[0]
        from .run_view import OutputScreen
        self.app.push_screen(OutputScreen(run_id))
