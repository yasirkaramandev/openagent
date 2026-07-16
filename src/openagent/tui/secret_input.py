"""Shared lifecycle guarantees for TUI password inputs."""

from __future__ import annotations

import contextlib

from textual.widgets import Input
from textual.worker import Worker, WorkerState


class SecretInputMixin:
    """Wipe secret widgets after terminal worker states and every screen exit path.

    Subclasses may override :meth:`_clear_secret_state` when they also retain a ``SecretStr`` or
    another in-memory copy. The mixin deliberately reacts to successful workers too: connection
    tests and probes receive a local copy for their own lifetime, then the form requires explicit
    re-entry instead of retaining a credential indefinitely.
    """

    secret_input_ids: tuple[str, ...] = ("api_key",)

    def clear_secret_material(self) -> None:
        for widget_id in self.secret_input_ids:
            with contextlib.suppress(Exception):
                self.query_one(f"#{widget_id}", Input).value = ""  # type: ignore[attr-defined]
        self._clear_secret_state()

    def _clear_secret_state(self) -> None:
        """Drop a non-widget copy; overridden by stateful wizards."""

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state in {WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED}:
            self.clear_secret_material()
