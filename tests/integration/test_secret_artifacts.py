"""No secret reaches any run artifact — prompt, diff, logs, or result (spec §30)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import RuntimeType
from openagent.credentials.redaction import clear_registered_secrets, register_secret

PROMPT_SECRET = "sk-promptLEAK1234567890abcdEF"
FILE_SECRET = "ghp_fileLEAK1234567890abcdefGHIJ"
PREFIXLESS = "haidian0099887766prefixlesskey"  # only caught via register_secret


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


@pytest.fixture()
def app(tmp_path: Path) -> OpenAgentApp:
    clear_registered_secrets()
    project = tmp_path / "proj"
    project.mkdir()
    _git(["init", "-q"], project)
    _git(["config", "user.email", "t@t.com"], project)
    _git(["config", "user.name", "t"], project)
    (project / "main.py").write_text("x = 1\n")
    _git(["add", "-A"], project)
    _git(["commit", "-q", "-m", "init"], project)
    paths = Paths(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db", project_root=project,
    )
    yield OpenAgentApp(paths)
    clear_registered_secrets()


async def test_no_secret_in_any_artifact(app: OpenAgentApp, httpx_mock: HTTPXMock):
    app.providers.add(name="testco", provider_type="custom", base_url="https://api.test/v1",
                      api_key="sk-x", store_key=False)
    register_secret(PREFIXLESS)  # a prefixless provider key active for this run
    app.agents.create(name="a", runtime_type=RuntimeType.API_AGENT, provider="testco",
                      model="m", permission_profile="safe-edit")

    # Turn 1: model writes a file whose contents include secrets. Turn 2: final answer echoes one.
    httpx_mock.add_response(content=_sse(
        {"id": "c1", "choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "t1", "function": {
                "name": "write_file",
                "arguments": json.dumps({
                    "path": "config.py",
                    "content": f"TOKEN='{FILE_SECRET}'\nOTHER='{PREFIXLESS}'\n",
                })}}]}}]},
    ), headers={"content-type": "text/event-stream"})
    httpx_mock.add_response(content=_sse(
        {"id": "c2", "choices": [{"delta": {"content": f"wrote key {FILE_SECRET} and {PREFIXLESS}"}}]},
    ), headers={"content-type": "text/event-stream"})

    run = app.runs.create(agent_name="a", prompt=f"store this secret: {PROMPT_SECRET}")
    await app.runs.execute(run)

    run_dir = app.paths.run_dir(run.id)
    for name in ("request.json", "status.json", "result.json", "events.jsonl",
                 "logs.txt", "output.md", "handoff.md", "changes.diff"):
        text = (run_dir / name).read_text()
        for secret in (PROMPT_SECRET, FILE_SECRET, PREFIXLESS):
            assert secret not in text, f"{secret} leaked into {name}"

    # The prompt was redacted in request.json but the run still ran.
    assert "[REDACTED]" in (run_dir / "request.json").read_text()
    # The change really happened (so the diff would have contained the secret unredacted).
    assert "config.py" in json.loads((run_dir / "result.json").read_text())["files_changed"]


async def test_cli_stderr_secret_is_redacted(app: OpenAgentApp, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch):
    """A secret a CLI writes to stderr must be scrubbed from the failed run's artifacts."""
    from tests.fakecli import FakeCliAdapter, write_fake_script

    adapter = FakeCliAdapter(write_fake_script(tmp_path), mode="leak_stderr")
    monkeypatch.setattr("openagent.services.run_service.build_cli_adapter",
                        lambda cli, executable=None: adapter)
    app.agents.create(name="cli-a", runtime_type=RuntimeType.CLI, cli="fake")

    run = app.runs.create(agent_name="cli-a", prompt="go", worktree="auto")
    result = await app.runs.execute(run)
    assert result.status.value == "failed"  # nonzero exit, no success event

    run_dir = app.paths.run_dir(run.id)
    for name in ("events.jsonl", "logs.txt", "result.json"):
        assert "ghp_stderrLEAK1234567890abcdefGHIJ" not in (run_dir / name).read_text()
