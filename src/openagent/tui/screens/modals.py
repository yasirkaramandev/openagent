"""Reusable modal dialogs: confirm, in-place warning, approval, and the agent's question.

Two rules hold across all of them:

* every externally supplied string (a command, a reason, a model's question) goes through
  :func:`safe_markup` — a model that writes ``[green]✓ approved[/green]`` must not be able to paint
  fake UI inside the very dialog asking whether to trust it (item 14);
* **Ctrl+C cancels the whole run**, not just the dialog (item 9). A modal is a place a run is
  *blocked*, so the cancel binding has to reach past it; the app raises the run's cancellation flag
  first and then releases the modal, so the unblocked worker finds the run already cancelled.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static
from textual.widgets.button import ButtonVariant

from ...security.approvals import ApprovalRequest
from ..markup import safe_markup


class ConfirmModal(ModalScreen[bool]):
    """A yes/no confirmation. Dismisses with ``True`` (confirm) or ``False`` (cancel)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    ConfirmModal { align: center middle; }
    ConfirmModal #box { width: 60; height: auto; border: round $warning; background: $panel; padding: 1 2; }
    ConfirmModal #buttons { height: 3; align-horizontal: right; }
    ConfirmModal Button { margin: 0 0 0 2; }
    """

    def __init__(
        self,
        question: str,
        *,
        confirm_label: str = "Confirm",
        confirm_variant: ButtonVariant = "error",
    ) -> None:
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
        Binding("ctrl+c", "cancel_run", "Cancel run"),
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
            yield Static(f"[b]Action:[/b] {safe_markup(r.action, 200)}")
            yield Static(f"[b]Reason:[/b] {safe_markup(r.reason, 300) or '—'}")
            yield Static(f"[b]Workspace:[/b] {safe_markup(r.workspace, 200) or '—'}", classes="k")
            # The command is the thing being judged — it must be shown verbatim and inert.
            yield Static(safe_markup(r.command or r.detail, 1000), id="cmd")
            with Horizontal(id="buttons"):
                yield Button("Deny (n)", id="deny")
                yield Button("Approve once (y)", variant="warning", id="approve")
            yield Static("[dim]Ctrl+C cancels the whole run[/dim]", classes="k")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "approve")

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)

    def action_cancel_run(self) -> None:
        """Ctrl+C while an approval is pending cancels the run itself (item 9)."""

        self.app.cancel_active_run(self.request.run_id)  # type: ignore[attr-defined]


class QuestionModal(ModalScreen[str | None]):
    """Ask the interactive user a question during a run (item 16).

    Dismisses with the typed answer, or ``None`` if the user *skips* (Esc) — the run then continues
    with the agent's best judgment. **Ctrl+C is different**: it cancels the whole run (item 9).
    """

    BINDINGS = [
        Binding("escape", "cancel", "Skip"),
        Binding("ctrl+c", "cancel_run", "Cancel run"),
    ]
    DEFAULT_CSS = """
    QuestionModal { align: center middle; }
    QuestionModal #box { width: 78; height: auto; border: thick $accent; background: $panel; padding: 1 2; }
    QuestionModal #q { text-style: bold; padding: 0 0 1 0; }
    QuestionModal #buttons { height: 3; align-horizontal: right; }
    QuestionModal Button { margin: 0 0 0 2; }
    QuestionModal .k { color: $text-muted; }
    """

    def __init__(self, question: str, run_id: str = "") -> None:
        super().__init__()
        self.question = question
        self.run_id = run_id

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("The agent is asking:", id="title")
            # The question comes from the model: escape it (item 14).
            yield Static(safe_markup(self.question, 1000), id="q")
            yield Input(placeholder="type your answer", id="answer")
            with Horizontal(id="buttons"):
                yield Button("Skip (Esc)", id="cancel")
                yield Button("Answer", variant="primary", id="ok")
            yield Static("[dim]Ctrl+C cancels the whole run[/dim]", classes="k")

    def on_mount(self) -> None:
        self.query_one("#answer", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit()
        else:
            self.dismiss(None)

    def _submit(self) -> None:
        value = self.query_one("#answer", Input).value.strip()
        self.dismiss(value or None)

    def action_cancel(self) -> None:
        """Esc *skips* the question; the run continues with the agent's best judgment."""

        self.dismiss(None)

    def action_cancel_run(self) -> None:
        """Ctrl+C cancels the run itself — the waiting worker must not simply carry on (item 9)."""

        self.app.cancel_active_run(self.run_id)  # type: ignore[attr-defined]


class InPlaceConfirmModal(ModalScreen[bool]):
    """Explicit confirmation before an editing agent runs directly in the user's project (item 8).

    The TUI used to pass ``confirm_in_place=(worktree == "none")`` — i.e. it answered the safety
    question *on the user's behalf* by restating the choice they had already made. Choosing "no
    isolation" is not the same as being told what that means and agreeing to it.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    InPlaceConfirmModal { align: center middle; }
    InPlaceConfirmModal #box { width: 72; height: auto; border: thick $error; background: $panel; padding: 1 2; }
    InPlaceConfirmModal #title { color: $error; text-style: bold; }
    InPlaceConfirmModal #buttons { height: 3; align-horizontal: right; }
    InPlaceConfirmModal Button { margin: 0 0 0 2; }
    InPlaceConfirmModal .k { color: $text-muted; }
    """

    def __init__(self, agent: str, workspace: str, profile: str) -> None:
        super().__init__()
        self.agent = agent
        self.workspace = workspace
        self.profile = profile

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("⚠ Run in place, with no isolation?", id="title")
            yield Static(
                "This agent will edit the current project directly.\n\n"
                "No isolated worktree or directory copy will be used. Changes are applied to your "
                "files as the agent makes them, and there is no separate diff to review first.",
            )
            yield Static(f"[b]Agent:[/b] {safe_markup(self.agent, 80)}", classes="k")
            yield Static(
                f"[b]Profile:[/b] {safe_markup(self.profile, 40)} (can edit files)", classes="k"
            )
            yield Static(f"[b]Directory:[/b] {safe_markup(self.workspace, 200)}", classes="k")
            yield Static("\nContinue in place?")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Run In Place", variant="error", id="confirm")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)
