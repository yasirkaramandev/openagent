"""Pilot tests for the approval + confirm modals (spec §29, §31)."""

from __future__ import annotations

from pathlib import Path

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.security.approvals import ApprovalRequest
from openagent.tui.app import OpenAgentTUI
from openagent.tui.screens.modals import ApprovalModal, ConfirmModal, QuestionModal


def _tui(tmp_path: Path) -> OpenAgentTUI:
    project = tmp_path / "proj"
    project.mkdir()
    paths = Paths(data_dir=tmp_path / "data", config_dir=tmp_path / "config",
                  db_path=tmp_path / "data" / "openagent.db", project_root=project)
    return OpenAgentTUI(OpenAgentApp(paths))


async def test_approval_modal_shows_context_and_approves(tmp_path: Path):
    app = _tui(tmp_path)
    async with app.run_test() as pilot:
        result: dict = {}
        request = ApprovalRequest(run_id="r", action="run_command", detail="rm -rf build",
                                  command="rm -rf build", reason="recursive delete", workspace="/ws")
        app.push_screen(ApprovalModal(request), lambda v: result.setdefault("v", v))
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ApprovalModal)
        # The modal surfaces the command, reason, and workspace.
        rendered = "".join(str(w.render()) for w in modal.query("Static"))
        assert "rm -rf build" in rendered
        assert "recursive delete" in rendered
        assert "/ws" in rendered

        await pilot.click("#approve")
        await pilot.pause()
        assert result["v"] is True


async def test_approval_modal_denies_with_key(tmp_path: Path):
    app = _tui(tmp_path)
    async with app.run_test() as pilot:
        result: dict = {}
        request = ApprovalRequest(run_id="r", action="run_command", detail="curl evil",
                                  command="curl evil", reason="network")
        app.push_screen(ApprovalModal(request), lambda v: result.setdefault("v", v))
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert result["v"] is False


async def test_question_modal_returns_typed_answer(tmp_path: Path):
    app = _tui(tmp_path)
    async with app.run_test() as pilot:
        result: dict = {}
        app.push_screen(QuestionModal("which port?"), lambda v: result.setdefault("v", v))
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, QuestionModal)
        assert "which port?" in "".join(str(w.render()) for w in modal.query("Static"))
        from textual.widgets import Input
        modal.query_one("#answer", Input).value = "8080"
        await pilot.click("#ok")
        await pilot.pause()
        assert result["v"] == "8080"


async def test_question_modal_cancel_returns_none(tmp_path: Path):
    app = _tui(tmp_path)
    async with app.run_test() as pilot:
        result: dict = {}
        app.push_screen(QuestionModal("anything?"), lambda v: result.setdefault("v", v))
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert result["v"] is None


async def test_confirm_modal_roundtrip(tmp_path: Path):
    app = _tui(tmp_path)
    async with app.run_test() as pilot:
        result: dict = {}
        app.push_screen(ConfirmModal("Delete it?", confirm_label="Delete"),
                        lambda v: result.setdefault("v", v))
        await pilot.pause()
        await pilot.click("#ok")
        await pilot.pause()
        assert result["v"] is True
