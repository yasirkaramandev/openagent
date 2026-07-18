"""SQLite-authoritative, cross-process live event tailing for one run."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..core.events import NormalizedEvent, is_terminal_event_type
from ..storage.repositories import EventIndexRepository, MalformedEventBody

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

EventBatchHook = Callable[[list[NormalizedEvent]], Awaitable[None] | None]
WarningHook = Callable[[str], None]


class BoundedEventIds:
    """Insertion-ordered set with bounded memory for local/SQLite duplicate suppression."""

    def __init__(self, capacity: int = 10_000) -> None:
        self.capacity = max(1, capacity)
        self._values: OrderedDict[str, None] = OrderedDict()

    def add(self, event_id: str) -> bool:
        """Return ``True`` only when this id was not already present."""

        if event_id in self._values:
            self._values.move_to_end(event_id)
            return False
        self._values[event_id] = None
        while len(self._values) > self.capacity:
            self._values.popitem(last=False)
        return True

    def __contains__(self, event_id: object) -> bool:
        return event_id in self._values

    def __len__(self) -> int:
        return len(self._values)


class RunEventTailer:
    """Incrementally replay and follow events committed by any OpenAgent process.

    ``last_seq`` is the source of truth. ``PRAGMA data_version`` only avoids unnecessary queries;
    a periodic forced probe remains, because same-connection writes need not change data_version.
    """

    def __init__(
        self,
        run_id: str,
        repository: EventIndexRepository,
        on_events: EventBatchHook,
        *,
        on_warning: WarningHook | None = None,
        poll_interval_active: float = 0.15,
        poll_interval_idle: float = 0.5,
        poll_interval_long_idle: float = 1.0,
        batch_size: int = 500,
        seen_capacity: int = 10_000,
        force_probe_interval: float = 1.0,
    ) -> None:
        self.run_id = run_id
        self.repository = repository
        self.on_events = on_events
        self.on_warning = on_warning
        self.last_seq = 0
        self.seen_event_ids = BoundedEventIds(seen_capacity)
        self.poll_interval_active = poll_interval_active
        self.poll_interval_idle = poll_interval_idle
        self.poll_interval_long_idle = poll_interval_long_idle
        self.batch_size = max(1, batch_size)
        self.force_probe_interval = max(poll_interval_active, force_probe_interval)
        self._stop = asyncio.Event()
        self._version_connection: Connection | None = None
        self._data_version: int | None = None
        self._last_query_at = 0.0
        self._idle_polls = 0
        self._terminal_seen = False
        self._stopped = False
        self._last_page_row_count = 0

    async def initial_replay(self) -> list[NormalizedEvent]:
        """Replay all current rows once with keyset pagination, then remember the DB version."""

        replayed: list[NormalizedEvent] = []
        while not self._stop.is_set():
            batch = await self._read_one_page()
            if not batch:
                break
            replayed.extend(batch)
            await self._deliver(batch)
            if self._last_page_row_count < self.batch_size:
                break
        self._open_version_connection()
        self._data_version = self._read_data_version()
        self._last_query_at = time.monotonic()
        return replayed

    def mark_local(self, event: NormalizedEvent) -> bool:
        """Mark a low-latency app-local event before applying it to the UI projection."""

        if is_terminal_event_type(event.type):
            self._terminal_seen = True
        return self.seen_event_ids.add(event.id)

    async def poll_once(self, *, force: bool = False) -> list[NormalizedEvent]:
        """Read only rows after ``last_seq`` and deliver unseen event ids as one batch."""

        if self._stopped:
            return []
        self._open_version_connection()
        now = time.monotonic()
        current_version = self._read_data_version()
        changed = self._data_version is None or current_version != self._data_version
        periodic_probe = now - self._last_query_at >= self.force_probe_interval
        if not force and not changed and not periodic_probe:
            return []
        self._data_version = current_version
        self._last_query_at = now

        delivered: list[NormalizedEvent] = []
        while not self._stop.is_set():
            batch = await self._read_one_page()
            if not batch:
                break
            delivered.extend(batch)
            if self._last_page_row_count < self.batch_size:
                break
        if delivered:
            await self._deliver(delivered)
            self._idle_polls = 0
        else:
            self._idle_polls += 1
        return delivered

    async def run(self) -> None:
        """Adaptively poll until stopped; terminal events receive one forced final drain."""

        try:
            while not self._stop.is_set():
                events = await self.poll_once()
                if self._terminal_seen:
                    final = await self.poll_once(force=True)
                    if not final:
                        return
                if events:
                    delay = self.poll_interval_active
                elif self._idle_polls < 4:
                    delay = self.poll_interval_idle
                else:
                    delay = self.poll_interval_long_idle
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._close_version_connection()
            self._stopped = True

    async def stop(self) -> None:
        self._stop.set()
        self._close_version_connection()
        self._stopped = True

    async def _read_one_page(self) -> list[NormalizedEvent]:
        def read() -> tuple[list[tuple[int, NormalizedEvent]], MalformedEventBody | None]:
            rows: list[tuple[int, NormalizedEvent]] = []
            try:
                rows.extend(
                    self.repository.iter_events_after(
                        self.run_id,
                        self.last_seq,
                        self.batch_size,
                    )
                )
                return rows, None
            except MalformedEventBody as exc:
                return rows, exc

        rows, malformed = await asyncio.to_thread(read)
        self._last_page_row_count = self.batch_size if malformed is not None else len(rows)

        unseen: list[NormalizedEvent] = []
        for seq, event in rows:
            expected = self.last_seq + 1
            if seq != expected:
                self._warn(f"event sequence gap: expected {expected}, found {seq}")
            self.last_seq = max(self.last_seq, seq)
            if self.seen_event_ids.add(event.id):
                unseen.append(event)
                if is_terminal_event_type(event.type):
                    self._terminal_seen = True

        if malformed is not None:
            exc = malformed
            expected = self.last_seq + 1
            if exc.seq > expected:
                self._warn(f"event sequence gap: expected {expected}, found malformed {exc.seq}")
            self._warn(str(exc))
            self.last_seq = max(self.last_seq, exc.seq)
        return unseen

    async def _deliver(self, events: list[NormalizedEvent]) -> None:
        if not events or self._stop.is_set():
            return
        try:
            result = self.on_events(events)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - a subscriber cannot kill the authoritative tailer
            self._warn(f"run event subscriber failed: {str(exc)[:300]}")

    def _warn(self, detail: str) -> None:
        if self.on_warning is not None:
            with contextlib.suppress(Exception):
                self.on_warning(detail)

    def _open_version_connection(self) -> None:
        if self._version_connection is None:
            self._version_connection = self.repository.db.engine.connect()

    def _read_data_version(self) -> int:
        assert self._version_connection is not None
        return int(self._version_connection.exec_driver_sql("PRAGMA data_version").scalar_one())

    def _close_version_connection(self) -> None:
        if self._version_connection is not None:
            self._version_connection.close()
            self._version_connection = None
