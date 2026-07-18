"""Table screens for Agents, Providers, CLI tools, and Runs (spec §31)."""

from __future__ import annotations

from datetime import datetime, timezone

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static

from ...core.models import AgentProfile, enum_value
from ...core.projection import RunProjection
from ...runtimes.cli.registry import discover_cli_models
from ...services.provider_service import ProviderInUseError
from ..markup import safe_line, safe_markup


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
        with Horizontal(classes="action-bar"):
            yield Button("Refresh", id="table-refresh")
            yield Button("Back", id="table-back")
        yield Footer()

    def on_mount(self) -> None:
        self.reload()

    def action_refresh(self) -> None:
        self.reload()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "table-refresh":
            self.action_refresh()
        elif event.button.id == "table-back":
            self.app.pop_screen()

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
        yield Static(
            "Agents  ([b]Enter[/b] details · [b]R[/b] run · [b]E[/b] edit · "
            "[b]Del[/b] remove · [b]A[/b] add · [b]/[/b] search)",
            classes="screen-title",
        )
        yield Input(
            placeholder="filter by name/title/tag…  (Enter to apply, Esc to clear)", id="search"
        )
        with Horizontal(id="agents-body"):
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            yield Static("", id="details")
        with Horizontal(classes="action-bar"):
            yield Button("Details", id="agent-details")
            yield Button("Run", id="agent-run")
            yield Button("Edit", id="agent-edit")
            yield Button("Add", id="agent-add")
            yield Button("Back", id="agent-back")
        yield Footer()

    def on_mount(self) -> None:
        self.reload()

    def action_refresh(self) -> None:
        self.reload()

    def _agents(self) -> list[AgentProfile]:
        agents = list(self.app.oa.agents.list())  # type: ignore[attr-defined]
        if self._filter:
            f = self._filter.lower()
            agents = [
                a
                for a in agents
                if f in a.name.lower()
                or f in (a.title or "").lower()
                or any(f in t.lower() for t in a.tags)
            ]
        return agents

    def reload(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_columns("Name", "Title", "Runtime", "Provider/CLI", "Model", "Tags", "Profile")
        for a in self._agents():
            model = a.runtime.model or "—" if _runtime_label(a) == "api" else "—"
            table.add_row(
                a.name,
                a.title or "—",
                _runtime_label(a),
                _provider_or_cli(a),
                model,
                ", ".join(a.tags) or "—",
                a.permission_profile,
                key=a.name,
            )
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "agent-details": self.action_details,
            "agent-run": self.action_run,
            "agent-edit": self.action_edit,
            "agent-add": self.action_add,
            "agent-back": self.app.pop_screen,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()

    def _update_details(self) -> None:
        name = self._selected_name()
        panel = self.query_one("#details", Static)
        agent = self.app.oa.agents.get(name) if name else None  # type: ignore[attr-defined]
        if not agent:
            panel.update("[dim]no agent selected[/dim]")
            return
        rt = agent.runtime
        # Titles, descriptions, tags and system prompts are user-supplied: escape them before they
        # enter a markup-enabled widget (item 14).
        binding = (
            f"CLI: {safe_markup(rt.cli, 40)}"
            if _runtime_label(agent) == "cli"
            else f"Provider: {safe_markup(rt.provider, 40)}\nModel: {safe_markup(rt.model, 60)}"
        )
        panel.update(
            f"[b]{safe_markup(agent.title or agent.name, 80)}[/b]\n"
            f"[dim]{safe_markup(agent.name, 60)}[/dim]\n\n"
            f"Runtime: {_runtime_label(agent)}\n{binding}\n"
            f"Profile: {safe_markup(agent.permission_profile, 30)}\n"
            f"Tags: {safe_markup(', '.join(agent.tags), 120) or '—'}\n\n"
            f"[b]Description[/b]\n{safe_markup(agent.description, 400) or '—'}\n\n"
            f"[b]System prompt[/b]\n{safe_markup(agent.system_prompt, 400) or '—'}"
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
            from .run_console import RunSetupScreen

            self.app.push_screen(RunSetupScreen(preselect=name))

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
            ConfirmModal(
                f"Delete agent [b]{safe_markup(name, 60)}[/b]? This also updates OPENAGENT.md.",
                confirm_label="Delete",
            ),
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
            cred = (
                p.credential.type if isinstance(p.credential.type, str) else p.credential.type.value
            )
            table.add_row(
                p.name,
                p.provider_type,
                p.protocol.value,
                p.base_url or "(preset)",
                cred,
                key=p.name,
            )

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

        self.app.push_screen(
            ConfirmModal(
                f"Remove provider [b]{safe_markup(name, 60)}[/b]?", confirm_label="Remove"
            ),
            callback=done,
        )

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
    title_text = (
        "CLI Tools  ([b]R[/b] refresh · [b]C[/b] check updates · "
        "[b]U[/b] update selected · [b]A[/b] update all)"
    )
    BINDINGS = _TableScreen.BINDINGS + [
        Binding("c", "check_updates", "Check updates"),
        Binding("u", "update_selected", "Update selected"),
        Binding("a", "update_all", "Update all"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._model_counts: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self.title_text, classes="screen-title")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        with Horizontal(classes="action-bar"):
            yield Button("Refresh", id="cli-refresh")
            yield Button("Check Updates", id="cli-check")
            yield Button("Update Selected", id="cli-update")
            yield Button("Update All", id="cli-update-all")
            yield Button("Back", id="cli-back")
        yield Footer()

    def on_mount(self) -> None:
        import asyncio

        try:
            asyncio.get_event_loop().create_task(self._discover())
        except RuntimeError:  # pragma: no cover
            pass
        self.reload()

    async def _discover(self) -> None:
        installations = await self.app.oa.clis.discover(persist=True)  # type: ignore[attr-defined]
        for installation in installations:
            result = await discover_cli_models(installation.type, installation.executable)
            self._model_counts[installation.type] = (
                str(len(result.models)) if result.available else "manual/default"
            )
        self.reload()

    def populate(self, table: DataTable) -> None:
        table.add_columns(
            "Type",
            "Current",
            "Latest",
            "Update",
            "Update detail",
            "Source",
            "Active Executable",
            "Shadowed / conflicts",
            "Auth",
            "Models",
            "Adapter",
        )
        for c in self.app.oa.clis.list():  # type: ignore[attr-defined]
            auth = "yes" if c.authenticated else ("no" if c.authenticated is False else "?")
            label = f"{c.type}{' (exp)' if c.experimental else ''}"
            update = c.update_status
            table.add_row(
                label,
                c.version or "—",
                update.latest_version if update and update.latest_version else "—",
                update.state.value if update else "unknown",
                safe_line(update.detail, 500) if update and update.detail else "—",
                c.install_source.value,
                c.executable,
                safe_line("; ".join(c.shadowed_executables), 500) or "—",
                auth,
                self._model_counts.get(c.type, "?"),
                c.adapter,
                key=c.type,
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "cli-refresh": self.action_refresh,
            "cli-check": self.action_check_updates,
            "cli-update": self.action_update_selected,
            "cli-update-all": self.action_update_all,
            "cli-back": self.app.pop_screen,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()

    def _selected_type(self) -> str | None:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0:
            return None
        try:
            return str(table.get_row_at(table.cursor_row)[0]).removesuffix(" (exp)")
        except Exception:  # pragma: no cover - transient table rebuild
            return None

    def action_check_updates(self) -> None:
        self.notify("checking official update metadata…")
        self.run_worker(self._check_updates(), exclusive=True)

    async def _check_updates(self) -> None:
        await self.app.oa.clis.check_updates(refresh=True)  # type: ignore[attr-defined]
        self.reload()
        self.notify("CLI update check complete")

    def action_update_selected(self) -> None:
        cli_type = self._selected_type()
        if cli_type:
            self._confirm_update(cli_type)

    def action_update_all(self) -> None:
        self._confirm_update(None)

    def _confirm_update(self, cli_type: str | None) -> None:
        from .modals import ConfirmModal

        target = safe_markup(cli_type, 40) if cli_type else "all installed CLIs"

        def done(confirmed: bool | None) -> None:
            if confirmed:
                self.run_worker(self._perform_update(cli_type), exclusive=True)

        self.app.push_screen(
            ConfirmModal(
                f"Update [b]{target}[/b]? Active runs, unknown sources, and conflicts remain blocked.",
                confirm_label="Update",
            ),
            callback=done,
        )

    async def _perform_update(self, cli_type: str | None) -> None:
        try:
            if cli_type is None:
                results = await self.app.oa.clis.update_all()  # type: ignore[attr-defined]
            else:
                result = await self.app.oa.clis.update(cli_type)  # type: ignore[attr-defined]
                results = {cli_type: result}
        except Exception as exc:  # noqa: BLE001 - updater error is user-visible, screen stays alive
            self.notify(f"CLI update failed: {safe_line(exc, 500)}", severity="error", timeout=8)
            return
        self.reload()
        failed = [
            name
            for name, result in results.items()
            if result.status.state.value in {"blocked", "check_failed"}
        ]
        if failed:
            self.notify(
                f"update blocked/failed: {', '.join(failed)}",
                severity="error",
                timeout=8,
            )
        else:
            self.notify("CLI update complete", severity="information")


_ACTIVE_STATUSES = ("queued", "starting", "running", "waiting_approval")


class RunsScreen(_TableScreen):
    """Recent runs. Enter opens the Run Console — live for an active run, replayed for a finished
    one (item 10). A run in flight must never be shown as a completed-only Output screen."""

    title_text = "Runs  ([b]Enter[/b] open console · [b]C[/b] cancel)"
    BINDINGS = _TableScreen.BINDINGS + [
        Binding("enter", "open_console", "Open"),
        Binding("c", "cancel_run", "Cancel"),
    ]

    def on_mount(self) -> None:
        self.reload()
        self.set_interval(1.0, self.reload)

    def populate(self, table: DataTable) -> None:
        table.add_columns(
            "ID", "Agent", "Runtime", "Phase", "Status", "Elapsed", "Activity", "Files"
        )
        oa = self.app.oa  # type: ignore[attr-defined]
        runs = list(oa.runs.list(50))
        active_ids = [r.id for r in runs if enum_value(r.status) in _ACTIVE_STATUSES]
        sqlite_activity = oa.repos.event_index.latest_activity_events(active_ids)
        for r in runs:
            status = enum_value(r.status)
            agent = oa.agents.get(r.agent)
            runtime = "—"
            if agent is not None:
                rt = agent.runtime
                kind = rt.type if isinstance(rt.type, str) else rt.type.value
                runtime = (rt.cli or "cli") if kind == "cli" else (rt.model or rt.provider or "api")
            activity = "—"
            live = self.app.live_run(r.id)  # type: ignore[attr-defined]
            if live is not None:
                activity = live.projection.current_activity
            elif status in _ACTIVE_STATUSES:
                event = sqlite_activity.get(r.id)
                if event is not None:
                    activity_projection = RunProjection(r.id)
                    activity_projection.apply(event)
                    activity = activity_projection.current_activity or r.phase
                else:
                    activity = r.phase
            table.add_row(
                r.id,
                r.agent,
                safe_line(runtime, 20),
                r.phase,
                status,
                _elapsed(r),
                safe_line(activity, 32),
                str(len(r.files_changed)),
                key=r.id,
            )

    def _selected_run(self) -> str | None:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0:
            return None
        return str(table.get_row_at(table.cursor_row)[0])

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # The focused DataTable consumes Enter before the screen binding ever sees it, so open the
        # console from the table's own selection message too.
        self._open_console(str(event.row_key.value or ""))

    def action_open_console(self) -> None:
        run_id = self._selected_run()
        if run_id:
            self._open_console(run_id)

    def _open_console(self, run_id: str) -> None:
        if not run_id:
            return
        from .run_console import RunConsoleScreen

        self.app.push_screen(RunConsoleScreen(run_id))

    def action_cancel_run(self) -> None:
        run_id = self._selected_run()
        if not run_id:
            return
        self.app.cancel_active_run(run_id)  # type: ignore[attr-defined]
        self.notify(f"cancelling {run_id}…")
        self.set_timer(0.5, self.reload)


def _elapsed(run) -> str:
    end = run.completed_at or datetime.now(timezone.utc)
    seconds = max(0, int((end - run.started_at).total_seconds()))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"
