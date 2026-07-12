"""Reusable modal dialogs: confirm, and the run approval prompt (spec §29, §31)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static
from textual.widgets.button import ButtonVariant

from ...security.approvals import ApprovalRequest


class ConfirmModal(ModalScreen[bool]):
    """A yes/no confirmation. Dismisses with ``True`` (confirm) or ``False`` (cancel)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    ConfirmModal { align: center middle; }
    ConfirmModal #box { width: 60; height: auto; border: round $warning; background: $panel; padding: 1 2; }
    ConfirmModal #buttons { height: 3; align-horizontal: right; }
    ConfirmModal Button { margin: 0 0 0 2; }
    """

    def __init__(self, question: str, *, confirm_label: str = "Confirm",
                 confirm_variant: ButtonVariant = "error") -> None:
        super().__init__()
        self.question = question
        self.confirm_label = confirm_label
        self.confirm_variant = confirm_variant

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(self.question, id="question")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.confirm_label, variant=self.confirm_variant, id="ok")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "ok")

    def action_cancel(self) -> None:
        self.dismiss(False)


class ApprovalModal(ModalScreen[bool]):
    """Pause a run and ask the user to approve/deny a high-risk operation (spec §29).

    Dismisses with ``True`` (approve once) or ``False`` (deny).
    """

    BINDINGS = [
        Binding("escape", "deny", "Deny"),
        Binding("y", "approve", "Approve"),
        Binding("n", "deny", "Deny"),
    ]
    DEFAULT_CSS = """
    ApprovalModal { align: center middle; }
    ApprovalModal #box { width: 78; height: auto; border: thick $warning; background: $panel; padding: 1 2; }
    ApprovalModal .k { color: $text-muted; }
    ApprovalModal #cmd { color: $warning; text-style: bold; padding: 1 0; }
    ApprovalModal #buttons { height: 3; align-horizontal: right; }
    ApprovalModal Button { margin: 0 0 0 2; }
    """

    def __init__(self, request: ApprovalRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        r = self.request
        with Vertical(id="box"):
            yield Label("⚠ Approval required", id="title")
            yield Static(f"[b]Action:[/b] {r.action}")
            yield Static(f"[b]Reason:[/b] {r.reason or '—'}")
            yield Static(f"[b]Workspace:[/b] {r.workspace or '—'}", classes="k")
            yield Static(r.command or r.detail, id="cmd")
            with Horizontal(id="buttons"):
                yield Button("Deny (n)", id="deny")
                yield Button("Approve once (y)", variant="warning", id="approve")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "approve")

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)
