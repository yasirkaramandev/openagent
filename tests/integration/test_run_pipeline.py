"""End-to-end run pipeline for an API agent (mocked HTTP) — spec §27, §35, §50.10."""

import json
import subprocess
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import RunStatus, RuntimeType


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _sse(*chunks: dict) -> bytes:
    """Serialize chunks as an OpenAI-style SSE stream (what real providers send)."""
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


@pytest.fixture()
def app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    _git(["init", "-q"], project)
    _git(["config", "user.email", "t@t.com"], project)
    _git(["config", "user.name", "t"], project)
    (project / "main.py").write_text("value = 1\n")
    _git(["add", "-A"], project)
    _git(["commit", "-q", "-m", "init"], project)
    paths = Paths(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db",
        project_root=project,
    )
    return OpenAgentApp(paths)


async def test_full_api_run_produces_bundle(app: OpenAgentApp, httpx_mock: HTTPXMock):
    app.providers.add(
        name="testco",
        provider_type="custom",
        base_url="https://api.test/v1",
        api_key="sk-x",
    )
    app.agents.create(
        name="testco-coder",
        title="Test Coder",
        runtime_type=RuntimeType.API_AGENT,
        provider="testco",
        model="test-model",
        tags=["coder"],
        permission_profile="safe-edit",
    )

    # Turn 1: model asks to apply_patch. Turn 2: final answer, no tools. (SSE streaming)
    httpx_mock.add_response(
        content=_sse(
            {
                "id": "c1",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {
                                        "name": "apply_patch",
                                        "arguments": json.dumps(
                                            {
                                                "path": "main.py",
                                                "old_string": "value = 1",
                                                "new_string": "value = 2",
                                            }
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ],
            },
            {"id": "c1", "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
        ),
        headers={"content-type": "text/event-stream"},
    )
    httpx_mock.add_response(
        content=_sse(
            {"id": "c2", "choices": [{"delta": {"content": "Updated value to 2 in main.py."}}]},
            {"id": "c2", "usage": {"prompt_tokens": 120, "completion_tokens": 15}},
        ),
        headers={"content-type": "text/event-stream"},
    )

    run = app.runs.create(agent_name="testco-coder", prompt="set value to 2", worktree="auto")
    events = []
    result = await app.runs.execute(run, on_event=events.append)

    assert result.status == RunStatus.COMPLETED
    assert "main.py" in result.files_changed

    # The edit is in the isolated worktree, not the user's working tree.
    assert (app.paths.project_root / "main.py").read_text() == "value = 1\n"
    assert (Path(result.worktree) / "main.py").read_text() == "value = 2\n"

    # Standard artifact bundle exists.
    run_dir = app.paths.run_dir(run.id)
    for name in (
        "request.json",
        "status.json",
        "events.jsonl",
        "output.md",
        "result.json",
        "changes.diff",
        "tests.json",
        "handoff.md",
    ):
        assert (run_dir / name).exists(), f"missing {name}"

    result_json = json.loads((run_dir / "result.json").read_text())
    assert result_json["status"] == "completed"
    assert "main.py" in result_json["files_changed"]
    assert "value to 2" in result_json["summary"]

    # Live events were streamed.
    types = {e.type for e in events}
    assert "run.started" in types and "run.completed" in types and "tool.completed" in types


async def test_secrets_never_in_artifacts(app: OpenAgentApp, httpx_mock: HTTPXMock):
    app.providers.add(
        name="testco", provider_type="custom", base_url="https://api.test/v1", api_key="sk-x"
    )
    app.agents.create(
        name="a",
        runtime_type=RuntimeType.API_AGENT,
        provider="testco",
        model="m",
        permission_profile="safe-edit",
    )
    httpx_mock.add_response(
        content=_sse(
            {
                "id": "c",
                "choices": [
                    {"delta": {"content": "leaking OPENAI_API_KEY=sk-abcd1234567890EFGHIJ now"}}
                ],
            },
        ),
        headers={"content-type": "text/event-stream"},
    )
    run = app.runs.create(agent_name="a", prompt="hi")
    await app.runs.execute(run)
    run_dir = app.paths.run_dir(run.id)
    for name in ("events.jsonl", "output.md", "logs.txt", "result.json"):
        assert "sk-abcd1234567890EFGHIJ" not in (run_dir / name).read_text()
