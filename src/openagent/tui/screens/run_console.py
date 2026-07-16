"""Run Agent: setup + preflight (stage A) and the live Run Console (stage B) — items 2, 10, 11, 20.

The old screen was a form beside a ``RichLog``: it appended one grey line per event, showed nothing
about *why* the agent was doing what it was doing, could not be closed without killing the run, and
could not be reopened. It gave no basis for believing the agent was working correctly.

**Stage A — setup and preflight.** Pick the agent, the task, the workspace strategy and the
permission profile; the screen immediately shows what that agent actually *is* (runtime, executable,
detected version, auth state) and runs the readiness checklist. ``Run Agent`` re-runs preflight
itself, so a run can never start on an unready agent just because the user did not press
``Check Readiness`` first.

**Stage B — the live console.** A fixed status header, tabbed panels, and a fixed action bar. Every
panel renders from :class:`RunProjection`, so an update *replaces* the thing it updates instead of
appending another line: the plan is one checklist, a command is one card whose output is replaced by
its latest snapshot, a file is one row that turns red if its patch failed.

What is shown is what the backend published for the user: reasoning **summaries**, the plan, the
commands, the files, the searches, the messages. Never hidden chain-of-thought — OpenAgent does not
have it, does not ask for it, and does not store it.

The run itself is owned by the *app*, not by this screen (see ``tui/app.py``): leaving the console
does not cancel the run, and reopening it replays ``events.jsonl`` and then tails the live stream.
"""

from __future__ import annotations

from datetime import datetime, timezone

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.events import MouseScrollDown, MouseScrollUp
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from ...core.events import ItemStatus, NormalizedEvent
from ...core.models import enum_value
from ...core.permissions import get_profile, profile_names
from ...core.projection import Item, RunProjection
from ...runtimes.cli.registry import cli_registry_entries
from ...services.run_service import RunError
from ...workspaces.worktree import STRATEGIES
from ..markup import safe_line, safe_markup
from ..select_utils import selected_string
from .modals import InPlaceConfirmModal

#: How often the live console re-renders while events stream in (seconds). Coalescing keeps a chatty
#: backend from re-rendering the whole console on every single token.
REFRESH_INTERVAL = 0.2

_MARK = {
    ItemStatus.COMPLETED.value: "[green]✓[/green]",
    ItemStatus.FAILED.value: "[red]✗[/red]",
    ItemStatus.CANCELLED.value: "[yellow]○[/yellow]",
    ItemStatus.IN_PROGRESS.value: "[cyan]●[/cyan]",
    ItemStatus.PENDING.value: "[dim]○[/dim]",
}

_KIND_LABEL = {
    "reasoning": "Reasoning summary",
    "progress": "Progress",
    "plan": "Plan",
    "command": "Command",
    "file": "File",
    "tool": "Tool",
    "web_search": "Web search",
    "message": "Message",
}


def _mark(status: str) -> str:
    return _MARK.get(status, "[dim]•[/dim]")


def _elapsed(start: str, end: str = "") -> str:
    if not start:
        return "00:00"
    try:
        started = datetime.fromisoformat(start)
        finished = datetime.fromisoformat(end) if end else datetime.now(timezone.utc)
    except ValueError:  # pragma: no cover - malformed timestamp
        return "00:00"
    seconds = max(0, int((finished - started).total_seconds()))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


# =========================================================================== stage A: setup


class RunSetupScreen(Screen):
    """Choose what to run, see what that agent is, and prove it is ready (items 2, 7)."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+r", "check", "Check Readiness"),
    ]
    DEFAULT_CSS = """
    RunSetupScreen #body { height: 1fr; }
    RunSetupScreen .col { width: 1fr; padding: 0 2; }
    RunSetupScreen Label { margin: 1 0 0 0; text-style: bold; }
    RunSetupScreen #agent-info, RunSetupScreen #preflight {
        border: round $primary; padding: 0 1; height: auto; margin: 1 0 0 0;
    }
    RunSetupScreen #actions { height: 3; padding: 0 2; background: $panel; }
    RunSetupScreen #actions Button { margin: 0 2 0 0; }
    """

    def __init__(self, preselect: str | None = None) -> None:
        super().__init__()
        self._preselect = preselect
        self._entries: dict[str, object] = {}

    def compose(self) -> ComposeResult:
        oa = self.app.oa  # type: ignore[attr-defined]
        agents = [(a.name, a.name) for a in oa.agents.list()]
        agent_kwargs: dict = {}
        if self._preselect and any(v == self._preselect for _, v in agents):
            agent_kwargs["value"] = self._preselect

        yield Header()
        yield Static("Run Agent", classes="screen-title")
        with Horizontal(id="body"):
            with VerticalScroll(classes="col"):
                yield Label("Agent")
                yield Select(
                    agents, prompt="select an agent", id="agent", allow_blank=True, **agent_kwargs
                )
                yield Label("Task")
                yield Input(placeholder="describe the task", id="prompt")
                yield Label("Workspace strategy")
                yield Select(
                    [(s, s) for s in STRATEGIES], value="auto", id="worktree", allow_blank=False
                )
                yield Label("Permission profile")
                yield Select(
                    [(p, p) for p in profile_names()],
                    id="profile",
                    allow_blank=True,
                    prompt="(the agent's own profile)",
                )
                yield Label("Working directory")
                yield Static(safe_markup(str(oa.paths.project_root)), id="cwd")
            with VerticalScroll(classes="col"):
                yield Static("[dim]Select an agent to see its runtime.[/dim]", id="agent-info")
                yield Static("[dim]Readiness has not been checked yet.[/dim]", id="preflight")
        with Horizontal(id="actions", classes="action-bar"):
            yield Button("Check Readiness", id="check")
            yield Button("Run Agent", variant="primary", id="run")
            yield Button("Cancel", id="cancel")
        yield Footer()

    async def on_mount(self) -> None:
        self._entries = {e.type: e for e in await cli_registry_entries()}
        self._render_agent_info()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "agent":
            self._render_agent_info()
            self.query_one("#preflight", Static).update(
                "[dim]Readiness has not been checked yet.[/dim]"
            )

    # ------------------------------------------------------------------ agent info

    def _agent_name(self) -> str | None:
        return selected_string(self.query_one("#agent", Select))

    def _render_agent_info(self) -> None:
        panel = self.query_one("#agent-info", Static)
        name = self._agent_name()
        oa = self.app.oa  # type: ignore[attr-defined]
        agent = oa.agents.get(name) if name else None
        if agent is None:
            panel.update("[dim]Select an agent to see its runtime.[/dim]")
            return

        rt = agent.runtime
        rtype = rt.type if isinstance(rt.type, str) else rt.type.value
        lines = [
            f"[b]{safe_markup(agent.title or agent.name, 60)}[/b]",
            f"Agent: {safe_markup(agent.name, 60)}",
            f"Runtime: {'CLI' if rtype == 'cli' else 'API'}",
        ]
        if rtype == "cli":
            entry = self._entries.get(rt.cli or "")
            lines.append(f"CLI: {safe_markup(rt.cli, 40)}")
            if entry is not None:
                auth = {True: "authenticated", False: "not detected", None: "unknown"}[
                    entry.authenticated  # type: ignore[attr-defined]
                ]
                lines += [
                    f"Executable: {safe_markup(entry.executable or '(not found)', 90)}",  # type: ignore[attr-defined]
                    f"Detected version: {safe_markup(entry.version or 'unknown', 40)}",  # type: ignore[attr-defined]
                    f"Authentication: {auth}",
                    f"Status: {safe_markup(entry.status_label, 120)}",  # type: ignore[attr-defined]
                ]
        else:
            provider = oa.providers.get(rt.provider or "")
            lines += [
                f"Provider: {safe_markup(rt.provider, 40)}"
                + (
                    f" ({safe_markup(provider.provider_type, 30)})"
                    if provider
                    else " [red](missing)[/red]"
                ),
                f"Model: {safe_markup(rt.model or '(none)', 60)}",
            ]
        lines += [
            f"Permission profile: {safe_markup(self._profile() or agent.permission_profile, 30)}",
            f"Workspace strategy: {safe_markup(self._value('worktree') or 'auto', 20)}",
        ]
        panel.update("\n".join(lines))

    # ------------------------------------------------------------------ actions

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "check":
            self.action_check()
        elif event.button.id == "run":
            self._start()
        elif event.button.id == "cancel":
            self.app.pop_screen()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._start()

    def action_check(self) -> None:
        name = self._agent_name()
        if not name:
            self.notify("choose an agent first", severity="warning")
            return
        self.query_one("#preflight", Static).update("[dim]checking readiness…[/dim]")
        self.run_worker(self._check(name), exclusive=True)

    async def _check(self, name: str):
        oa = self.app.oa  # type: ignore[attr-defined]
        report = await oa.runs.preflight.check(
            agent_name=name,
            permission_profile=self._profile() or None,
        )
        colour = {True: "green", False: "red"}
        lines = [
            f"[{colour[c.ok if c.mandatory else True]}]{c.symbol}[/] {safe_line(c.name, 60)}"
            + (f": {safe_line(c.detail, 90)}" if c.detail else "")
            for c in report.checks
        ]
        verdict = (
            "[green]Ready to run.[/green]"
            if report.ok
            else "[red]Not ready — fix the ✗ items above.[/red]"
        )
        self.query_one("#preflight", Static).update("\n".join([*lines, "", verdict]))
        return report

    def _start(self) -> None:
        name = self._agent_name()
        prompt = self.query_one("#prompt", Input).value.strip()
        if not name or not prompt:
            self.notify("choose an agent and enter a task", severity="warning")
            return
        self.run_worker(self._preflight_then_run(name, prompt), exclusive=True)

    async def _preflight_then_run(self, name: str, prompt: str) -> None:
        """Run Agent always preflights first — pressing Check Readiness is a convenience, not a gate."""

        report = await self._check(name)
        if not report.ok:
            self.notify(
                "readiness checks failed — the run was not started", severity="error", timeout=8
            )
            return

        oa = self.app.oa  # type: ignore[attr-defined]
        strategy = self._value("worktree") or "auto"
        profile_name = self._profile() or oa.agents.get(name).permission_profile
        confirmed = True
        if strategy == "none" and get_profile(profile_name).can_edit_files:
            # Explicit, informed confirmation — never inferred from the strategy alone (item 8).
            modal = InPlaceConfirmModal(
                agent=name, workspace=str(oa.paths.project_root), profile=profile_name
            )
            confirmed = bool(await self.app.push_screen_wait(modal))  # type: ignore[arg-type]
        if not confirmed:
            self.notify("run cancelled — nothing was started")
            return

        try:
            run = oa.runs.create(
                agent_name=name,
                prompt=prompt,
                worktree=strategy,
                permission_profile=profile_name,
                confirm_in_place=confirmed,
            )
        except RunError as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return

        self.app.start_run(run)  # type: ignore[attr-defined]
        self.app.switch_screen(RunConsoleScreen(run.id))

    def _profile(self) -> str:
        return selected_string(self.query_one("#profile", Select)) or ""

    def _value(self, wid: str) -> str | None:
        return selected_string(self.query_one(f"#{wid}", Select))


# =========================================================================== stage B: live console


class RunConsoleScreen(Screen):
    """The live Run Console: status header, tabbed detail panels, fixed action bar (items 2, 11)."""

    BINDINGS = [
        Binding("escape", "leave", "Back (keeps running)"),
        Binding("ctrl+c", "cancel_run", "Cancel run"),
        Binding("f", "follow_up", "Follow-up"),
    ]
    DEFAULT_CSS = """
    RunConsoleScreen #status { height: auto; max-height: 6; padding: 0 1; border: round $primary; }
    RunConsoleScreen #console-tabs { height: 1fr; }
    RunConsoleScreen #actions { height: 3; padding: 0 1; background: $panel; }
    RunConsoleScreen #actions Button { margin: 0 1 0 0; }
    RunConsoleScreen .pane { height: 1fr; padding: 0 1; }
    RunConsoleScreen #overview-body { height: 1fr; }
    RunConsoleScreen #timeline, RunConsoleScreen #details { width: 1fr; padding: 0 1; }
    RunConsoleScreen TextArea { height: 1fr; }
    RunConsoleScreen #followup-row { height: auto; padding: 0 1; }
    """

    #: (tab id, label). Short labels so all ten still fit/scroll at 80 columns.
    TABS = [
        ("overview", "Overview"),
        ("reasoning", "Reasoning"),
        ("plan", "Plan"),
        ("commands", "Commands"),
        ("files", "Files"),
        ("diff", "Diff"),
        ("tests", "Tests"),
        ("messages", "Messages"),
        ("usage", "Usage"),
        ("raw", "Raw Events"),
    ]

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id
        self.projection = RunProjection(run_id)
        self._dirty = True
        self._live = None
        self.follow_output = True

    # ------------------------------------------------------------------ compose

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="status")
        with TabbedContent(id="console-tabs"):
            for tab_id, label in self.TABS:
                with TabPane(label, id=f"tab-{tab_id}"):
                    if tab_id == "overview":
                        with Horizontal(id="overview-body"):
                            with VerticalScroll(id="timeline"):
                                yield Static("", id="timeline-body")
                            with VerticalScroll(id="details"):
                                yield Static("", id="details-body")
                    elif tab_id in ("diff", "raw"):
                        yield TextArea(id=f"pane-{tab_id}", read_only=True, soft_wrap=True)
                    else:
                        with VerticalScroll(classes="pane"):
                            yield Static("", id=f"pane-{tab_id}")
        with Horizontal(id="followup-row"):
            yield Input(placeholder="follow-up prompt…", id="followup")
        with Horizontal(id="actions", classes="action-bar"):
            yield Button("Cancel Run", variant="error", id="cancel")
            yield Button("Follow-up", id="follow")
            yield Button("Back", id="back")
        yield Footer()

    def on_mount(self) -> None:
        oa = self.app.oa  # type: ignore[attr-defined]
        self.query_one("#followup-row").display = False

        # 1) Replay the persisted log — this is what makes a run reopenable, live or finished.
        self.projection = oa.runs.projection(self.run_id)
        # 2) Then subscribe to the live stream, if this app is the one running it.
        self._live = self.app.live_run(self.run_id)  # type: ignore[attr-defined]
        if self._live is not None:
            # Adopt the in-memory projection so nothing that arrived between replay and subscribe is
            # lost, and keep receiving updates.
            self.projection = self._live.projection
            self._live.subscribe(self._on_event)
        self._dirty = True
        self.set_interval(REFRESH_INTERVAL, self._refresh_if_dirty)
        self.set_interval(1.0, self._tick)
        self._render_all()

    def on_unmount(self) -> None:
        if self._live is not None:
            self._live.unsubscribe(self._on_event)

    # ------------------------------------------------------------------ live updates

    def _on_event(self, event: NormalizedEvent) -> None:
        self._dirty = True

    def _tick(self) -> None:
        self._render_status()  # the elapsed clock keeps moving even when nothing happens

    def _refresh_if_dirty(self) -> None:
        if self._dirty:
            self._dirty = False
            self._render_all()

    def on_tabbed_content_tab_activated(self, event) -> None:
        self._dirty = True

    # ------------------------------------------------------------------ rendering

    def _render_all(self) -> None:
        self._render_status()
        p = self.projection
        self.query_one("#timeline-body", Static).update(self._timeline(p))
        self.query_one("#details-body", Static).update(self._details(p))
        self.query_one("#pane-reasoning", Static).update(self._reasoning(p))
        self.query_one("#pane-plan", Static).update(self._plan(p))
        self.query_one("#pane-commands", Static).update(self._commands(p))
        self.query_one("#pane-files", Static).update(self._files(p))
        self.query_one("#pane-tests", Static).update(self._tests(p))
        self.query_one("#pane-messages", Static).update(self._messages(p))
        self.query_one("#pane-usage", Static).update(self._usage(p))
        self._render_artifacts()
        self._render_actions()
        if self.follow_output:
            self.call_after_refresh(self._scroll_output_to_end)

    def _scroll_output_to_end(self) -> None:
        for container in self.query(VerticalScroll):
            if container.region.height > 0:
                container.scroll_end(animate=False)

    def _active_scroll_container(self) -> VerticalScroll | None:
        focused = self.focused
        if focused is not None:
            for widget in focused.ancestors_with_self:
                if isinstance(widget, VerticalScroll) and widget.region.height > 0:
                    return widget
        return next(
            (widget for widget in self.query(VerticalScroll) if widget.region.height > 0), None
        )

    def _sync_follow_output(self) -> None:
        target = self._active_scroll_container()
        self.follow_output = bool(
            target is None or target.max_scroll_y == 0 or target.scroll_y >= target.max_scroll_y
        )

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        self.follow_output = False

    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        self.call_after_refresh(self._sync_follow_output)

    def on_key(self, event) -> None:
        if event.key in {"pageup", "home"}:
            self.follow_output = False
        elif event.key == "end":
            self.follow_output = True
        elif event.key == "pagedown":
            self.call_after_refresh(self._sync_follow_output)

    def _render_status(self) -> None:
        oa = self.app.oa  # type: ignore[attr-defined]
        run = oa.runs.get(self.run_id)
        p = self.projection
        agent = oa.agents.get(run.agent) if run else None
        rt = agent.runtime if agent else None
        rtype = ""
        if rt is not None:
            kind = rt.type if isinstance(rt.type, str) else rt.type.value
            rtype = rt.cli or "" if kind == "cli" else f"{rt.provider or ''}/{rt.model or ''}"

        status = p.status or (run.status if run else "")
        status = enum_value(status)
        colour = {"completed": "green", "failed": "red", "cancelled": "yellow"}.get(status, "cyan")
        phase = p.phase or (run.phase if run else "")
        elapsed = _elapsed(
            p.started_at or (run.started_at.isoformat() if run else ""), p.completed_at
        )
        workspace = p.workspace or (run.worktree or run.workspace if run else "")

        self.query_one("#status", Static).update(
            f"Agent: [b]{safe_line(run.agent if run else '?', 24)}[/b]   "
            f"Status: [{colour}]{safe_line(status or phase, 18)}[/{colour}]   "
            f"Elapsed: {elapsed}   Turn: {run.turns if run else 1}\n"
            f"Runtime: {safe_line(rtype, 40)}   Phase: {safe_line(phase, 20)}   "
            f"Profile: {safe_line(run.permission_profile if run else '', 16)}\n"
            f"Workspace: {safe_line(workspace, 90)}"
        )

    def _timeline(self, p: RunProjection) -> str:
        if not p.items:
            return "[dim]waiting for the agent…[/dim]"
        lines = []
        current_turn = 0
        for item in p.items[-200:]:
            if item.turn != current_turn:
                current_turn = item.turn
                lines.append(f"\n[b]── Turn {current_turn} ──[/b]")
            lines.append(f"{_mark(item.status)} {safe_line(self._title(item), 70)}")
        return "\n".join(lines)

    def _title(self, item: Item) -> str:
        label = _KIND_LABEL.get(item.kind, item.kind)
        if item.kind in ("reasoning", "progress"):
            return f"{label}: {item.text}"
        if item.kind == "command":
            return f"$ {item.command}"
        if item.kind == "message":
            return f"{label}: {item.text}"
        return item.title or label

    def _details(self, p: RunProjection) -> str:
        """The right-hand pane: what the agent is thinking about and what it plans to do."""

        blocks: list[str] = []
        reasoning = p.reasoning
        blocks.append("[b]Reasoning summary[/b]")
        if reasoning:
            blocks.append(safe_markup(reasoning[-1].text, 600))
        else:
            blocks.append("[dim]No summary published yet.[/dim]")

        blocks.append("\n[b]Current plan[/b]")
        blocks.append(self._plan(p, compact=True))

        active = [i for i in p.items if i.status == ItemStatus.IN_PROGRESS.value]
        if active:
            blocks.append("\n[b]In progress[/b]")
            blocks += [f"{_mark(i.status)} {safe_line(self._title(i), 60)}" for i in active[-5:]]
        if p.error:
            blocks.append("\n[b][red]Failure[/red][/b]")
            blocks.append(
                f"[red]{safe_markup(p.error.get('error_type'), 40)}[/red]: "
                f"{safe_markup(p.error.get('message'), 400)}"
            )
            if p.error.get("phase"):
                blocks.append(f"[dim]phase: {safe_markup(p.error.get('phase'), 30)}[/dim]")
        return "\n".join(blocks)

    def _reasoning(self, p: RunProjection) -> str:
        items = p.reasoning
        if not items:
            return (
                "[dim]This backend has not published a reasoning summary yet.\n\n"
                "If it never does, operational activity (commands, files, tools) is shown instead — "
                "OpenAgent shows only what a backend exposes as user-visible, and never hidden "
                "chain-of-thought.[/dim]"
            )
        out = []
        for item in items:
            label = _KIND_LABEL.get(item.kind, item.kind)
            out.append(f"{_mark(item.status)} [b]{label}[/b]  [dim](turn {item.turn})[/dim]")
            out.append(safe_markup(item.text, 2000))
            out.append("")
        return "\n".join(out)

    def _plan(self, p: RunProjection, *, compact: bool = False) -> str:
        plan = p.plan
        if not plan:
            return "[dim]No plan published.[/dim]"
        marks = {True: "[green]✓[/green]", False: "[dim]○[/dim]"}
        lines = [f"{marks[step.completed]} {safe_line(step.text, 70)}" for step in plan]
        if compact:
            return "\n".join(lines)
        done = sum(1 for s in plan if s.completed)
        return "\n".join([f"[b]{done}/{len(plan)} complete[/b]", "", *lines])

    def _commands(self, p: RunProjection) -> str:
        if not p.commands:
            return "[dim]No commands run.[/dim]"
        out = []
        for item in p.commands:
            exit_label = "—" if item.exit_code is None else str(item.exit_code)
            out.append(f"{_mark(item.status)} [b]$ {safe_line(item.command, 90)}[/b]")
            out.append(f"   [dim]status {safe_line(item.status, 20)} · exit {exit_label}[/dim]")
            if item.output:
                # The latest snapshot replaces the previous one — the whole aggregated buffer is not
                # appended again every time Codex re-sends it (item 5).
                body = safe_markup(item.output[-2000:])
                out += [f"   {line}" for line in body.splitlines()]
            out.append("")
        return "\n".join(out)

    def _files(self, p: RunProjection) -> str:
        if not p.files:
            return "[dim]No files changed.[/dim]"
        colour = {"created": "green", "modified": "yellow", "deleted": "red"}
        out = []
        for item in p.files:
            c = colour.get(item.change, "white")
            failed = " [red](patch failed)[/red]" if item.failed else ""
            out.append(
                f"{_mark(item.status)} [{c}]{safe_line(item.change, 12)}[/{c}] "
                f"{safe_line(item.path, 80)}{failed}"
            )
        return "\n".join(out)

    def _tests(self, p: RunProjection) -> str:
        tests = p.tests
        if not tests.get("ran"):
            return "[dim]No tests were run.[/dim]"
        verdict = "[green]passed[/green]" if tests.get("passed") else "[red]failed[/red]"
        return (
            f"Command: {safe_line(tests.get('command'), 90)}\n"
            f"Result: {verdict} (exit {tests.get('exit_code')})"
        )

    def _messages(self, p: RunProjection) -> str:
        if not p.messages:
            return "[dim]No messages yet.[/dim]"
        out = []
        for item in p.messages:
            out.append(f"[dim]── turn {item.turn} ──[/dim]")
            out.append(safe_markup(item.text, 4000))
            out.append("")
        return "\n".join(out)

    def _usage(self, p: RunProjection) -> str:
        u = p.usage
        if not u:
            return "[dim]No usage reported.[/dim]"
        lines = [
            f"Input tokens:     {u.get('input_tokens', 0)}",
            f"Cached input:     {u.get('cached_input_tokens', 0)}",
            f"Output tokens:    {u.get('output_tokens', 0)}",
            f"Reasoning tokens: {u.get('reasoning_tokens', 0)}",
        ]
        if u.get("provider_cost") is not None:
            lines.append(f"Provider cost:    {u['provider_cost']}")
        lines.append("")
        lines.append(
            "[dim]Reasoning tokens are counted by the provider. Their content is never "
            "requested or stored.[/dim]"
        )
        return "\n".join(lines)

    def _render_artifacts(self) -> None:
        oa = self.app.oa  # type: ignore[attr-defined]
        diff = self.query_one("#pane-diff", TextArea)
        try:
            diff.text = oa.runs.output(self.run_id, "diff") or "(no changes)"
        except RunError:
            diff.text = "The diff is collected when the run finishes."

        raw = self.query_one("#pane-raw", TextArea)
        try:
            lines = oa.runs.output(self.run_id, "events").splitlines()
        except RunError:
            lines = []
        header = (
            "# Diagnostic output. Redaction already happened before these lines were written "
            "to disk.\n"
        )
        raw.text = header + "\n".join(lines[-500:])

    def _render_actions(self) -> None:
        oa = self.app.oa  # type: ignore[attr-defined]
        run = oa.runs.get(self.run_id)
        if run is None:
            return
        status = enum_value(run.status)
        active = status in ("queued", "starting", "running", "waiting_approval")
        self.query_one("#cancel", Button).disabled = not active
        can_resume, _ = oa.runs.resume_support(run)
        self.query_one("#follow", Button).disabled = not can_resume

    # ------------------------------------------------------------------ actions

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel_run()
        elif event.button.id == "follow":
            self.action_follow_up()
        elif event.button.id == "back":
            self.action_leave()

    def action_leave(self) -> None:
        """Close the console. The run keeps going — it is owned by the app, not by this screen."""

        self.app.pop_screen()

    def action_cancel_run(self) -> None:
        self.app.cancel_active_run(self.run_id)  # type: ignore[attr-defined]
        self.notify("cancelling the run…")

    def action_follow_up(self) -> None:
        oa = self.app.oa  # type: ignore[attr-defined]
        run = oa.runs.get(self.run_id)
        if run is None:
            return
        ok, why = oa.runs.resume_support(run)
        if not ok:
            self.notify(why, severity="warning", timeout=8)
            return
        row = self.query_one("#followup-row")
        row.display = True
        self.query_one("#followup", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "followup":
            return
        prompt = event.value.strip()
        if not prompt:
            return
        event.input.value = ""
        self.query_one("#followup-row").display = False
        self.app.resume_run(self.run_id, prompt)  # type: ignore[attr-defined]
        # Disable follow-up the instant the worker starts (§4.1), rather than waiting for the next
        # refresh tick to notice the run went active. The per-run lock in RunService is the real
        # guarantee — a second follow-up is rejected outright — but the UI must not offer an action
        # it knows will be refused. ``_render_actions`` re-evaluates once the turn reaches a terminal
        # state and follow-up becomes available again.
        self.query_one("#follow", Button).disabled = True
        self._live = self.app.live_run(self.run_id)  # type: ignore[attr-defined]
        if self._live is not None:
            self.projection = self._live.projection
            self._live.subscribe(self._on_event)
        self._dirty = True
        self.notify("follow-up sent — turn 2 continues in the same session")
