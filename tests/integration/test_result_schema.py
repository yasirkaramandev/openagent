"""The generated result.json validates against schemas/result.schema.json (item 19)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import jsonschema

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import RunStatus, RuntimeType
from tests.fakecli import FakeCliAdapter, install_fake_cli, write_fake_script

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "schemas" / "result.schema.json").read_text()
)


def _app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()

    def g(*a):
        subprocess.run(["git", *a], cwd=str(project), check=True, capture_output=True)

    g("init", "-q")
    g("config", "user.email", "t@t.com")
    g("config", "user.name", "t")
    (project / "seed.txt").write_text("seed\n")
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    paths = Paths(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db",
        project_root=project,
    )
    oa = OpenAgentApp(paths)
    oa.agents.create(name="fake-coder", runtime_type=RuntimeType.CLI, cli="fake")
    return oa


async def test_result_json_matches_schema_initial_and_resumed(tmp_path: Path, monkeypatch):
    adapter = FakeCliAdapter(write_fake_script(tmp_path), mode="complete", resume_mode="resume")
    install_fake_cli(monkeypatch, adapter)
    oa = _app(tmp_path)

    run = oa.runs.create(agent_name="fake-coder", prompt="do it", worktree="auto")
    result = await oa.runs.execute(run)
    assert result.status == RunStatus.COMPLETED

    initial = json.loads(oa.runs.output(run.id, "json"))
    jsonschema.validate(initial, SCHEMA)
    assert initial["turns"] == 1
    assert "turns" in initial and "usage" in initial

    await oa.runs.resume(run.id, "again")
    resumed = json.loads(oa.runs.output(run.id, "json"))
    jsonschema.validate(resumed, SCHEMA)
    assert resumed["turns"] == 2
