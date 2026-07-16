"""Projected run state — the current view of an append-only event log (item 3).

``events.jsonl`` is append-only: an item that changes (a plan being ticked off, a command finishing,
a patch failing) produces a *new* event rather than rewriting an old one. Readers therefore need a
projection: fold the event stream into "what is true now", keyed by ``(source, turn, item_id)``.

One projection serves three consumers, so they can never disagree:

* the live **Run Console** (updates the existing card instead of appending duplicate lines),
* **timeline.md** (the human-readable narrative of a run),
* the structured fields of **result.json** (reasoning summaries, plan, commands, web searches, turns).

Replaying the whole log rebuilds the exact same state, which is what makes closing and reopening a
live run — or reattaching after a restart — work (item 10).

Nothing here interprets hidden reasoning: a ``reasoning.summary`` carries text the *backend* marked
as a user-visible summary, and that is all this module ever stores or shows (item 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .events import EventType, ItemStatus, NormalizedEvent, RunPhase

#: Per-item bound on retained command output, so one chatty command can't grow the projection without
#: limit. Persisted logs keep their own (also bounded) copy.
MAX_ITEM_OUTPUT_CHARS = 20_000

#: Bound on how many timeline entries are retained in memory for very long runs.
MAX_TIMELINE_ITEMS = 2_000


def _etype(event: NormalizedEvent) -> str:
    return event.type if isinstance(event.type, str) else event.type.value


@dataclass
class PlanItem:
    text: str
    completed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "completed": self.completed}


@dataclass
class Item:
    """One addressable thing the agent did, projected to its latest state."""

    #: ``(source, turn, item_id)``. The **turn** is part of the key because a backend may restart its
    #: item numbering each turn — Codex does: turn 2's first message is ``item_0`` again. Keyed only
    #: by ``(source, item_id)``, turn 2's answer would overwrite turn 1's card instead of appearing
    #: as a new one, and the console would show a single turn that mysteriously changed its mind.
    key: tuple[str, int, str]
    kind: str  # reasoning | progress | plan | command | file | tool | web_search | message
    status: str = ItemStatus.IN_PROGRESS.value
    title: str = ""
    text: str = ""  # reasoning/progress/message text
    command: str = ""
    output: str = ""
    exit_code: int | None = None
    path: str = ""
    change: str = ""  # created | modified | deleted
    tool: str = ""
    query: str = ""
    plan: list[PlanItem] = field(default_factory=list)
    turn: int = 1
    timestamp: str = ""
    seq: int = 0  # first-seen order, so a projected update keeps its original slot

    @property
    def item_id(self) -> str:
        return self.key[2]

    @property
    def source(self) -> str:
        return self.key[0]

    @property
    def failed(self) -> bool:
        return self.status in (ItemStatus.FAILED.value, ItemStatus.CANCELLED.value)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "item_id": self.item_id,
            "source": self.source,
            "kind": self.kind,
            "status": self.status,
            "turn": self.turn,
            "timestamp": self.timestamp,
        }
        for name in ("title", "text", "command", "output", "path", "change", "tool", "query"):
            value = getattr(self, name)
            if value:
                out[name] = value
        if self.exit_code is not None:
            out["exit_code"] = self.exit_code
        if self.plan:
            out["plan"] = [p.to_dict() for p in self.plan]
        return out


@dataclass
class TurnView:
    """One turn of a run (turn 1 = the initial prompt; +1 per resume, spec §32)."""

    number: int
    prompt: str = ""
    started_at: str = ""
    status: str = ""
    item_keys: list[tuple[str, int, str]] = field(default_factory=list)


class RunProjection:
    """Folds a run's normalized events into the current state of that run.

    Append the events in order (live) or replay them from ``events.jsonl`` (reopen/restart) — both
    produce the same projection.
    """

    def __init__(self, run_id: str = "") -> None:
        self.run_id = run_id
        self.phase: str = RunPhase.QUEUED.value
        self.status: str = ""
        self.agent: str = ""
        self.workspace: str = ""
        self.permission_profile: str = ""
        self.session_id: str | None = None
        self.pid: int | None = None
        self.started_at: str = ""
        self.completed_at: str = ""
        self.turn: int = 1
        self.usage: dict[str, Any] = {}
        self.error: dict[str, Any] = {}
        self.warnings: list[str] = []
        self.tests: dict[str, Any] = {}
        self.files_changed: list[str] = []

        self._items: dict[tuple[str, int, str], Item] = {}
        self._order: list[tuple[str, int, str]] = []
        self._seq = 0
        self._anon = 0
        self.turns: dict[int, TurnView] = {1: TurnView(number=1)}

    # ------------------------------------------------------------------ views

    @property
    def items(self) -> list[Item]:
        """Every projected item, in first-seen order (an update keeps its original slot)."""
        return [self._items[k] for k in self._order if k in self._items]

    def by_kind(self, *kinds: str) -> list[Item]:
        return [i for i in self.items if i.kind in kinds]

    @property
    def plan(self) -> list[PlanItem]:
        """The most recent plan (the agent maintains one checklist, updated in place)."""
        plans = self.by_kind("plan")
        return plans[-1].plan if plans else []

    @property
    def reasoning(self) -> list[Item]:
        return self.by_kind("reasoning", "progress")

    @property
    def commands(self) -> list[Item]:
        return self.by_kind("command")

    @property
    def files(self) -> list[Item]:
        return self.by_kind("file")

    @property
    def web_searches(self) -> list[Item]:
        return self.by_kind("web_search")

    @property
    def messages(self) -> list[Item]:
        return self.by_kind("message")

    @property
    def final_message(self) -> str:
        msgs = self.messages
        return msgs[-1].text if msgs else ""

    @property
    def current_activity(self) -> str:
        """A one-line description of what the agent is doing right now (Runs list, item 10)."""

        for item in reversed(self.items):
            if item.status == ItemStatus.IN_PROGRESS.value:
                return item.title or item.kind
        if self.phase in (
            RunPhase.COMPLETED.value,
            RunPhase.FAILED.value,
            RunPhase.CANCELLED.value,
        ):
            return self.phase
        last = self.items[-1] if self.items else None
        return (last.title or last.kind) if last else self.phase

    # ------------------------------------------------------------------ folding

    def apply_all(self, events: list[NormalizedEvent]) -> RunProjection:
        for event in events:
            self.apply(event)
        return self

    def apply(self, event: NormalizedEvent) -> Item | None:
        """Fold one event in. Returns the item it touched, or ``None`` for run-level events."""

        etype = _etype(event)
        data = event.data
        if not self.run_id:
            self.run_id = event.run_id

        handler = _RUN_LEVEL.get(etype)
        if handler is not None:
            handler(self, event, data)
            return None

        kind = _ITEM_KIND.get(etype)
        if kind is None:
            return None
        item = self._touch(event, kind)
        _ITEM_APPLY[etype](self, item, data)
        return item

    # ------------------------------------------------------------------ internals

    def _touch(self, event: NormalizedEvent, kind: str) -> Item:
        """Find (or create) the item this event addresses, keyed by ``(source, turn, item_id)``."""

        item_id = str(event.data.get("item_id") or "")
        if not item_id:
            # An adapter that doesn't itemize (or a one-shot event) still gets a stable slot.
            self._anon += 1
            item_id = f"_{kind}_{self._anon}"
        key = (event.source, self.turn, item_id)
        item = self._items.get(key)
        if item is None:
            self._seq += 1
            item = Item(key=key, kind=kind, seq=self._seq, turn=self.turn)
            self._items[key] = item
            self._order.append(key)
            self.turns.setdefault(self.turn, TurnView(number=self.turn)).item_keys.append(key)
            self._evict()
        item.timestamp = event.timestamp
        status = event.data.get("status")
        if status:
            item.status = str(status)
        title = event.data.get("title")
        if title:
            item.title = str(title)
        return item

    def _evict(self) -> None:
        while len(self._order) > MAX_TIMELINE_ITEMS:
            key = self._order.pop(0)
            self._items.pop(key, None)

    # -- run-level handlers -------------------------------------------------

    def _on_run_started(self, event: NormalizedEvent, data: dict) -> None:
        self.started_at = event.timestamp
        self.agent = str(data.get("agent") or self.agent)
        self.workspace = str(data.get("workspace") or self.workspace)
        self.permission_profile = str(data.get("permission_profile") or self.permission_profile)
        self.phase = RunPhase.QUEUED.value

    def _on_phase(self, event: NormalizedEvent, data: dict) -> None:
        phase = str(data.get("phase") or "")
        if phase:
            self.phase = phase

    def _on_process_started(self, event: NormalizedEvent, data: dict) -> None:
        pid = data.get("pid")
        if pid:
            self.pid = int(pid)

    def _on_session(self, event: NormalizedEvent, data: dict) -> None:
        sid = data.get("provider_session_id") or data.get("session_id")
        if sid:
            self.session_id = str(sid)
        turn = data.get("turn")
        if turn:
            self.turn = int(turn)
            view = self.turns.setdefault(self.turn, TurnView(number=self.turn))
            view.started_at = event.timestamp
            view.prompt = str(data.get("prompt") or view.prompt)

    def _on_usage(self, event: NormalizedEvent, data: dict) -> None:
        for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"):
            if data.get(key) is not None:
                self.usage[key] = int(self.usage.get(key, 0)) + int(data[key] or 0)
        cost = data.get("provider_cost")
        if cost is not None:
            self.usage["provider_cost"] = float(self.usage.get("provider_cost") or 0.0) + float(
                cost
            )

    def _on_tests(self, event: NormalizedEvent, data: dict) -> None:
        self.tests = {
            "ran": True,
            "passed": data.get("passed"),
            "exit_code": data.get("exit_code"),
            "command": data.get("command", ""),
        }

    def _on_terminal(self, event: NormalizedEvent, data: dict) -> None:
        etype = _etype(event)
        mapping = {
            EventType.RUN_COMPLETED.value: RunPhase.COMPLETED.value,
            EventType.RUN_FAILED.value: RunPhase.FAILED.value,
            EventType.RUN_CANCELLED.value: RunPhase.CANCELLED.value,
        }
        self.phase = self.status = mapping[etype]
        self.completed_at = event.timestamp
        if etype == EventType.RUN_FAILED.value:
            self.error = {
                "error_type": data.get("error_type") or "unknown",
                "message": data.get("message") or "",
                "phase": data.get("phase") or "",
                "source": event.source,
            }
        for turn in self.turns.values():
            if not turn.status:
                turn.status = self.status

    # -- item handlers ------------------------------------------------------

    def _apply_reasoning(self, item: Item, data: dict) -> None:
        text = str(data.get("text") or "").strip()
        if text:
            item.text = text
        if not item.title:
            item.title = "Reasoning summary"
        if not data.get("status"):
            item.status = ItemStatus.COMPLETED.value

    def _apply_progress(self, item: Item, data: dict) -> None:
        summary = str(data.get("summary") or data.get("text") or "").strip()
        next_step = str(data.get("next_step") or "").strip()
        item.text = "\n".join(x for x in (summary, next_step and f"Next: {next_step}") if x)
        item.title = "Progress"
        item.status = ItemStatus.COMPLETED.value

    def _apply_plan(self, item: Item, data: dict) -> None:
        raw = data.get("items") or []
        item.plan = [
            PlanItem(text=str(x.get("text") or ""), completed=bool(x.get("completed")))
            for x in raw
            if isinstance(x, dict)
        ]
        done = sum(1 for p in item.plan if p.completed)
        item.title = f"Plan ({done}/{len(item.plan)})"
        if not data.get("status"):
            item.status = (
                ItemStatus.COMPLETED.value
                if item.plan and done == len(item.plan)
                else ItemStatus.IN_PROGRESS.value
            )

    def _apply_command_started(self, item: Item, data: dict) -> None:
        item.command = str(data.get("command") or "")
        item.title = item.command
        item.status = str(data.get("status") or ItemStatus.IN_PROGRESS.value)

    def _apply_command_output(self, item: Item, data: dict) -> None:
        """Apply a command's output.

        Codex re-sends the whole ``aggregated_output`` each time, so an adapter marks it as a
        *snapshot*: it replaces the visible output for that item instead of appending the full buffer
        over and over (item 5). A true incremental chunk appends.
        """

        chunk = str(data.get("output") or "")
        if data.get("snapshot"):
            item.output = chunk[-MAX_ITEM_OUTPUT_CHARS:]
        elif chunk:
            item.output = (item.output + chunk)[-MAX_ITEM_OUTPUT_CHARS:]
        if data.get("command"):
            item.command = str(data["command"])

    def _apply_command_completed(self, item: Item, data: dict) -> None:
        if data.get("command"):
            item.command = str(data["command"])
            item.title = item.command
        exit_code = data.get("exit_code")
        item.exit_code = int(exit_code) if exit_code is not None else None
        output = data.get("output")
        if output is not None:
            item.output = str(output)[-MAX_ITEM_OUTPUT_CHARS:]
        if not data.get("status"):
            item.status = (
                ItemStatus.COMPLETED.value
                if item.exit_code in (0, None)
                else ItemStatus.FAILED.value
            )

    def _apply_file(self, item: Item, data: dict) -> None:
        item.path = str(data.get("path") or "")
        item.change = str(data.get("change") or "")
        item.title = f"{item.change or 'changed'} {item.path}".strip()
        if not data.get("status"):
            item.status = ItemStatus.COMPLETED.value

    def _apply_tool(self, item: Item, data: dict) -> None:
        item.tool = str(data.get("tool") or data.get("name") or "")
        server = str(data.get("server") or "")
        item.title = f"{server}/{item.tool}" if server else item.tool
        args = data.get("arguments_summary") or data.get("result_summary") or data.get("error")
        if args:
            item.text = str(args)
        if not data.get("status"):
            item.status = ItemStatus.COMPLETED.value

    def _apply_web_search(self, item: Item, data: dict) -> None:
        query = str(data.get("query") or "")
        if query:
            item.query = query
        item.title = f"Web search: {item.query}" if item.query else "Web search"
        if not data.get("status"):
            item.status = ItemStatus.COMPLETED.value

    def _apply_message(self, item: Item, data: dict) -> None:
        item.text = str(data.get("text") or "")
        item.title = "Message"
        item.status = ItemStatus.COMPLETED.value


_RUN_LEVEL: dict[str, Any] = {
    EventType.RUN_STARTED.value: RunProjection._on_run_started,
    EventType.RUN_PHASE.value: RunProjection._on_phase,
    EventType.PROCESS_STARTED.value: RunProjection._on_process_started,
    EventType.SESSION_CREATED.value: RunProjection._on_session,
    EventType.SESSION_RESUMED.value: RunProjection._on_session,
    EventType.USAGE_UPDATED.value: RunProjection._on_usage,
    EventType.TEST_COMPLETED.value: RunProjection._on_tests,
    EventType.RUN_COMPLETED.value: RunProjection._on_terminal,
    EventType.RUN_FAILED.value: RunProjection._on_terminal,
    EventType.RUN_CANCELLED.value: RunProjection._on_terminal,
}

_ITEM_KIND: dict[str, str] = {
    EventType.REASONING_SUMMARY.value: "reasoning",
    EventType.PROGRESS_UPDATED.value: "progress",
    EventType.PLAN_UPDATED.value: "plan",
    EventType.COMMAND_STARTED.value: "command",
    EventType.COMMAND_OUTPUT.value: "command",
    EventType.COMMAND_COMPLETED.value: "command",
    EventType.FILE_CREATED.value: "file",
    EventType.FILE_MODIFIED.value: "file",
    EventType.FILE_DELETED.value: "file",
    EventType.TOOL_STARTED.value: "tool",
    EventType.TOOL_COMPLETED.value: "tool",
    EventType.TOOL_FAILED.value: "tool",
    EventType.WEB_SEARCH_STARTED.value: "web_search",
    EventType.WEB_SEARCH_COMPLETED.value: "web_search",
    EventType.MESSAGE_COMPLETED.value: "message",
}

_ITEM_APPLY: dict[str, Any] = {
    EventType.REASONING_SUMMARY.value: RunProjection._apply_reasoning,
    EventType.PROGRESS_UPDATED.value: RunProjection._apply_progress,
    EventType.PLAN_UPDATED.value: RunProjection._apply_plan,
    EventType.COMMAND_STARTED.value: RunProjection._apply_command_started,
    EventType.COMMAND_OUTPUT.value: RunProjection._apply_command_output,
    EventType.COMMAND_COMPLETED.value: RunProjection._apply_command_completed,
    EventType.FILE_CREATED.value: RunProjection._apply_file,
    EventType.FILE_MODIFIED.value: RunProjection._apply_file,
    EventType.FILE_DELETED.value: RunProjection._apply_file,
    EventType.TOOL_STARTED.value: RunProjection._apply_tool,
    EventType.TOOL_COMPLETED.value: RunProjection._apply_tool,
    EventType.TOOL_FAILED.value: RunProjection._apply_tool,
    EventType.WEB_SEARCH_STARTED.value: RunProjection._apply_web_search,
    EventType.WEB_SEARCH_COMPLETED.value: RunProjection._apply_web_search,
    EventType.MESSAGE_COMPLETED.value: RunProjection._apply_message,
}
