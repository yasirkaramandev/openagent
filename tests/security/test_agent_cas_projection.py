"""Agent update/remove use compare-and-swap, and treat OPENAGENT.md as a committed projection.

Before this, ``AgentService.update``/``remove`` wrote unconditionally and, on a document-sync
failure, rolled the database back by re-writing the previous row. Two failures followed from that:
a stale in-memory copy could overwrite a newer committed state, and a projection conflict could
*undo* a committed change — potentially discarding what another process had committed in between.
The database is the source of truth (spec §8, §9): the write is a CAS, and a projection conflict is
deferred to the journal + doctor, never rolled back.
"""

from __future__ import annotations

from pathlib import Path

from openagent.app import OpenAgentApp
from openagent.config import OPENAGENT_MD_START, Paths
from openagent.core.models import RuntimeType
from openagent.storage.repositories import ConcurrentModificationError


def _paths(tmp_path: Path) -> Paths:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return Paths(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db",
        project_root=project,
    )


def _corrupt_document(app: OpenAgentApp) -> None:
    """Leave OPENAGENT.md with a start marker but no end marker: an unresolvable conflict that
    ``write_openagent_md`` refuses rather than guessing, so the projection cannot complete."""

    app.paths.openagent_md().write_text(
        f"hand-written prose\n{OPENAGENT_MD_START}\n(no end marker)\n", encoding="utf-8"
    )


def _make_cli_agent(app: OpenAgentApp, name: str) -> None:
    app.agents.create(name=name, runtime_type=RuntimeType.CLI, cli="codex")


def test_get_with_revision_returns_profile_and_revision(tmp_path: Path) -> None:
    app = OpenAgentApp(_paths(tmp_path))
    _make_cli_agent(app, "coder")
    read = app.repos.agents.get_with_revision("coder")
    assert read is not None
    agent, revision = read
    assert agent.name == "coder"
    assert revision == 0


def test_delete_checked_is_compare_and_swap(tmp_path: Path) -> None:
    app = OpenAgentApp(_paths(tmp_path))
    _make_cli_agent(app, "coder")

    try:
        app.repos.agents.delete_checked("coder", expected_revision=999)
    except ConcurrentModificationError:
        pass
    else:
        raise AssertionError("stale revision should have raised ConcurrentModificationError")

    assert app.repos.agents.get("coder") is not None  # nothing deleted on the failed CAS
    app.repos.agents.delete_checked("coder", expected_revision=0)  # correct revision
    assert app.repos.agents.get("coder") is None


def test_update_commits_and_defers_projection_on_conflict(tmp_path: Path) -> None:
    app = OpenAgentApp(_paths(tmp_path))
    _make_cli_agent(app, "coder")
    _corrupt_document(app)

    # The document cannot be regenerated, but the update must still commit and must not raise.
    updated = app.agents.update("coder", title="Renamed")

    assert updated.title == "Renamed"
    assert app.repos.agents.get("coder").title == "Renamed"  # DB is authoritative, committed
    pending = app.journal.pending()
    assert [op.kind for op in pending] == ["agent_document_sync"]  # deferred, not rolled back


def test_remove_commits_and_is_not_rolled_back_on_projection_conflict(tmp_path: Path) -> None:
    app = OpenAgentApp(_paths(tmp_path))
    _make_cli_agent(app, "coder")
    _corrupt_document(app)

    removed = app.agents.remove("coder")

    assert removed is True
    # The old code re-inserted the agent on a projection failure; the committed delete must stand.
    assert app.repos.agents.get("coder") is None
    assert [op.kind for op in app.journal.pending()] == ["agent_document_sync"]


def test_deferred_projection_is_retried_at_startup_once_the_document_is_fixed(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    app = OpenAgentApp(paths)
    _make_cli_agent(app, "coder")
    _corrupt_document(app)
    app.agents.update("coder", title="Renamed")
    assert app.journal.pending()  # a projection is owed

    # The user repairs the document, then a fresh process starts: recovery retries the sync.
    paths.openagent_md().unlink()
    restarted = OpenAgentApp(paths)
    assert restarted.journal.pending() == []
    assert "Renamed" in paths.openagent_md().read_text(encoding="utf-8")
