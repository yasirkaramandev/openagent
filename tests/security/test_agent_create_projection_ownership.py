"""Regression for create-time projection compensation deleting a later writer's agent."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import OPENAGENT_MD_START, Paths

pytestmark = [pytest.mark.security, pytest.mark.multiprocess]


_CREATE_WORKER = textwrap.dedent(
    """
    import json
    import sys
    import time
    from pathlib import Path

    from openagent.app import OpenAgentApp
    from openagent.config import Paths
    from openagent.core.models import RuntimeType
    from openagent.reporting.openagent_md import OpenAgentMdConflict

    root = Path(sys.argv[1])
    paths = Paths(
        data_dir=root / "data",
        config_dir=root / "config",
        db_path=root / "data" / "openagent.db",
        project_root=root / "project",
    )
    app = OpenAgentApp(paths)

    def blocked_projection():
        (root / "a_inserted").write_text("ready", encoding="utf-8")
        deadline = time.monotonic() + 20
        while not (root / "b_committed").exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("timed out waiting for updater")
            time.sleep(0.01)
        raise OpenAgentMdConflict(paths.openagent_md(), "forced projection conflict")

    app.agents.sync_openagent_md = blocked_projection
    try:
        created = app.agents.create(
            name="coder",
            title="created-by-a",
            runtime_type=RuntimeType.CLI,
            cli="codex",
        )
        print(json.dumps({"outcome": "returned", "title": created.title}))
    except Exception as exc:
        print(json.dumps({"outcome": "raised", "type": type(exc).__name__}))
    """
)


_UPDATE_WORKER = textwrap.dedent(
    """
    import json
    import sys
    import time
    from pathlib import Path

    from openagent.app import OpenAgentApp
    from openagent.config import Paths

    root = Path(sys.argv[1])
    deadline = time.monotonic() + 20
    while not (root / "a_inserted").exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("timed out waiting for creator")
        time.sleep(0.01)
    paths = Paths(
        data_dir=root / "data",
        config_dir=root / "config",
        db_path=root / "data" / "openagent.db",
        project_root=root / "project",
    )
    app = OpenAgentApp(paths)
    updated = app.agents.update("coder", title="committed-by-b", system_prompt="keep-me")
    (root / "b_committed").write_text("done", encoding="utf-8")
    print(json.dumps({"title": updated.title}))
    """
)


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    source = str(Path("src").resolve())
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    return env


def test_create_projection_conflict_cannot_delete_a_later_process_update(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    paths = Paths(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db",
        project_root=project,
    )
    # Keep startup recovery and both projections pending instead of letting either process repair
    # the document. The database remains authoritative throughout.
    paths.openagent_md().write_text(
        f"user prose\n{OPENAGENT_MD_START}\nmissing end marker\n", encoding="utf-8"
    )

    creator_script = tmp_path / "creator.py"
    updater_script = tmp_path / "updater.py"
    creator_script.write_text(_CREATE_WORKER, encoding="utf-8")
    updater_script.write_text(_UPDATE_WORKER, encoding="utf-8")

    creator = subprocess.Popen(
        [sys.executable, str(creator_script), str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_environment(),
    )
    updater = subprocess.Popen(
        [sys.executable, str(updater_script), str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_environment(),
    )
    updater_stdout, updater_stderr = updater.communicate(timeout=30)
    creator_stdout, creator_stderr = creator.communicate(timeout=30)

    assert updater.returncode == 0, updater_stderr
    assert creator.returncode == 0, creator_stderr
    assert json.loads(updater_stdout)["title"] == "committed-by-b"
    assert json.loads(creator_stdout)["outcome"] == "returned"

    restarted = OpenAgentApp(paths)
    read = restarted.repos.agents.get_with_revision("coder")
    assert read is not None, "create compensation deleted the later process's committed row"
    survivor, revision = read
    assert revision == 1
    assert survivor.title == "committed-by-b"
    assert survivor.system_prompt == "keep-me"
    assert any(op.kind == "agent_document_sync" for op in restarted.journal.pending())
