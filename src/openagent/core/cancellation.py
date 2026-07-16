"""Per-run cancellation (item 9).

Cancelling a CLI run was already real (kill the process tree). Cancelling an **API** run was not: the
agent loop had no idea it had been cancelled, so it kept calling the provider, kept running tools, and
could still finish ``completed`` after the user pressed Cancel.

A :class:`RunCancellation` is the one object both sides share. The run loop checks it at every point
where it could otherwise keep going:

* before each provider request, and while consuming the provider stream;
* before and after every tool call;
* before every new agent-loop step;
* while waiting for an approval or an ``ask_user`` answer;
* during finalization.

**Threading.** In the TUI the run executes in a thread worker with its *own* event loop, while
``cancel()`` is called from the UI's event loop. ``asyncio.Event`` is not safe across loops, so the
authoritative flag is a :class:`threading.Event` (safe from anywhere) and the awaitable mirror is set
through ``loop.call_soon_threadsafe``. Checking cancellation therefore works from any thread, and
*waiting* for it works inside the run's own loop.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")

DEFAULT_REASON = "cancelled by user"


class RunCancelled(Exception):
    """Raised inside a run once cancellation has been requested."""

    def __init__(self, reason: str = DEFAULT_REASON) -> None:
        super().__init__(reason)
        self.reason = reason


class RunCancellation:
    """The cancellation flag for one run, shared between the canceller and the run loop."""

    def __init__(self, run_id: str = "") -> None:
        self.run_id = run_id
        self.reason: str | None = None
        self._flag = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None

    # ------------------------------------------------------------------ setup

    def bind(self) -> None:
        """Attach to the event loop the run is executing on. Called from inside that loop."""

        self._loop = asyncio.get_running_loop()
        self._event = asyncio.Event()
        if self._flag.is_set():  # cancelled before the run even got going
            self._event.set()

    # ------------------------------------------------------------------ requesting

    def cancel(self, reason: str = DEFAULT_REASON) -> None:
        """Request cancellation. Safe from any thread and any event loop. Idempotent."""

        if self._flag.is_set():
            return
        self.reason = reason
        self._flag.set()
        loop, event = self._loop, self._event
        if loop is None or event is None:
            return
        try:
            if not loop.is_closed():
                loop.call_soon_threadsafe(event.set)
        except RuntimeError:  # pragma: no cover - loop torn down mid-cancel
            pass

    # ------------------------------------------------------------------ observing

    @property
    def cancelled(self) -> bool:
        return self._flag.is_set()

    def check(self) -> None:
        """Raise :class:`RunCancelled` if cancellation was requested. The loop's checkpoint."""

        if self._flag.is_set():
            raise RunCancelled(self.reason or DEFAULT_REASON)

    async def wait(self) -> None:
        if self._event is None:
            self.bind()
        assert self._event is not None
        await self._event.wait()

    async def guard(self, awaitable: Awaitable[T]) -> T:
        """Await ``awaitable``, but abandon it the moment cancellation is requested.

        The losing task is cancelled, so a provider stream or a pending request is torn down rather
        than left running in the background.
        """

        self.check()
        if self._event is None:
            self.bind()
        work = asyncio.ensure_future(awaitable)
        waiter = asyncio.ensure_future(self.wait())
        try:
            done, _ = await asyncio.wait({work, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                return work.result()
            work.cancel()
            try:
                await work
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown only
                pass
            raise RunCancelled(self.reason or DEFAULT_REASON)
        finally:
            waiter.cancel()


class CancellationRegistry:
    """The live :class:`RunCancellation` for each in-flight run, keyed by ``run_id``."""

    def __init__(self) -> None:
        self._controllers: dict[str, RunCancellation] = {}
        self._lock = threading.Lock()

    def create(self, run_id: str) -> RunCancellation:
        with self._lock:
            controller = RunCancellation(run_id)
            self._controllers[run_id] = controller
            return controller

    def get(self, run_id: str) -> RunCancellation | None:
        with self._lock:
            return self._controllers.get(run_id)

    def cancel(self, run_id: str, reason: str = DEFAULT_REASON) -> bool:
        """Request cancellation of ``run_id``. Returns False when no live controller exists."""

        controller = self.get(run_id)
        if controller is None:
            return False
        controller.cancel(reason)
        return True

    def discard(self, run_id: str) -> None:
        with self._lock:
            self._controllers.pop(run_id, None)
