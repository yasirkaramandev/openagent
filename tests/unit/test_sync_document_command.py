"""``openagent agent sync-document`` is the way out of an OPENAGENT.md conflict (spec §33).

The command exists because the writer now refuses to regenerate a document whose markers it cannot
interpret — that refusal protects the user's prose, but only if there is a supported way to see
what OpenAgent would write and to resolve the conflict deliberately. These tests pin the two
properties that make it usable rather than another dead end:

* ``--dry-run`` never writes, and answers **even on a conflicted document** — the one situation the
  error message tells the user to run it in;
* applying it regenerates the file and preserves the surrounding prose.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from openagent.app import OpenAgentApp
from openagent.cli.app import app
from openagent.config import OPENAGENT_MD_END, OPENAGENT_MD_START, Paths

runner = CliRunner()
USER_PROSE = "Context that exists nowhere but this file."


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    paths = Paths(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db",
        project_root=root,
    )
    # Every _app() in the command resolves to this project.
    monkeypatch.setattr(
        "openagent.cli.app.OpenAgentApp.create", classmethod(lambda cls: OpenAgentApp(paths))
    )
    return root


def test_dry_run_on_a_healthy_document_shows_a_diff_and_writes_nothing(project: Path) -> None:
    document = project / "OPENAGENT.md"
    document.write_text(
        f"# Notes\n\n{USER_PROSE}\n\n{OPENAGENT_MD_START}\nold\n{OPENAGENT_MD_END}\n",
        encoding="utf-8",
    )
    before = document.read_bytes()

    result = runner.invoke(app, ["agent", "sync-document", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert document.read_bytes() == before, "a dry run modified the file"


def test_dry_run_answers_on_a_conflicted_document(project: Path) -> None:
    """The circular-error regression: --dry-run must not itself refuse on a conflict.

    The writer refuses a document with a start marker and no end marker, and the error tells the
    user to run ``--dry-run`` to preview. If --dry-run also refused, that instruction would be a
    loop with no exit.
    """

    document = project / "OPENAGENT.md"
    document.write_text(
        f"# Notes\n\n{USER_PROSE}\n\n{OPENAGENT_MD_START}\ntruncated\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["agent", "sync-document", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "would be replaced" in result.output
    assert "# OpenAgent" in result.output, "the replacement document should be shown"
    assert USER_PROSE in document.read_text(encoding="utf-8"), "the file must be untouched"


def test_apply_regenerates_and_preserves_prose(project: Path) -> None:
    document = project / "OPENAGENT.md"
    document.write_text(
        f"# Notes\n\n{USER_PROSE}\n\n{OPENAGENT_MD_START}\nold\n{OPENAGENT_MD_END}\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["agent", "sync-document"])

    assert result.exit_code == 0, result.output
    assert USER_PROSE in document.read_text(encoding="utf-8")


def test_apply_on_a_conflicted_document_fails_without_writing(project: Path) -> None:
    """Applying (not previewing) must still refuse a conflict — silence would overwrite prose."""

    document = project / "OPENAGENT.md"
    document.write_text(
        f"# Notes\n\n{USER_PROSE}\n\n{OPENAGENT_MD_START}\ntruncated\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["agent", "sync-document"])

    assert result.exit_code == 1
    assert USER_PROSE in document.read_text(encoding="utf-8")
    assert "sync-document" in result.output
