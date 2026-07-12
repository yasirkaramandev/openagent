"""End-to-end: an API agent's ask_user call receives the interactive answer (item 16)."""

from __future__ import annotations

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
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


@pytest.fixture()
def app(tmp_path: Path) -> OpenAgentApp:
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
    return OpenAgentApp(paths)


async def test_ask_user_answer_reaches_model_and_is_recorded(app: OpenAgentApp, httpx_mock: HTTPXMock):
    app.providers.add(name="testco", provider_type="custom", base_url="https://api.test/v1",
                      api_key="sk-x", store_key=False)
    app.agents.create(name="asker", runtime_type=RuntimeType.API_AGENT,
                      provider="testco", model="test-model", permission_profile="safe-edit")

    # Turn 1: the model calls ask_user. Turn 2: it produces a final answer, no tools.
    httpx_mock.add_response(content=_sse(
        {"id": "c1", "choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_q", "function": {
                "name": "ask_user",
                "arguments": json.dumps({"question": "which port should I use?"})}}]}}]},
        {"id": "c1", "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
    ), headers={"content-type": "text/event-stream"})
    httpx_mock.add_response(content=_sse(
        {"id": "c2", "choices": [{"delta": {"content": "Using port 8080 as instructed."}}]},
        {"id": "c2", "usage": {"prompt_tokens": 12, "completion_tokens": 4}},
    ), headers={"content-type": "text/event-stream"})

    asked: list[str] = []

    def answer(question: str) -> str:
        asked.append(question)
        return "use port 8080"

    run = app.runs.create(agent_name="asker", prompt="pick a port", worktree="auto")
    result = await app.runs.execute(run, ask_user_callback=answer)

    assert result.status == RunStatus.COMPLETED
    assert asked == ["which port should I use?"]

    # The answer was fed back to the model on the second request.
    second_body = httpx_mock.get_requests()[1].content.decode()
    assert "use port 8080" in second_body

    # The Q&A is on the event stream.
    events = app.runs.output(run.id, "events")
    assert "which port should I use?" in events
    assert "question_answered" in events


async def test_ask_user_without_callback_uses_best_judgment(app: OpenAgentApp, httpx_mock: HTTPXMock):
    app.providers.add(name="testco", provider_type="custom", base_url="https://api.test/v1",
                      api_key="sk-x", store_key=False)
    app.agents.create(name="asker", runtime_type=RuntimeType.API_AGENT,
                      provider="testco", model="test-model", permission_profile="safe-edit")

    httpx_mock.add_response(content=_sse(
        {"id": "c1", "choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_q", "function": {
                "name": "ask_user",
                "arguments": json.dumps({"question": "anything?"})}}]}}]},
    ), headers={"content-type": "text/event-stream"})
    httpx_mock.add_response(content=_sse(
        {"id": "c2", "choices": [{"delta": {"content": "Proceeding with defaults."}}]},
    ), headers={"content-type": "text/event-stream"})

    run = app.runs.create(agent_name="asker", prompt="go", worktree="auto")
    # No ask_user_callback (non-interactive CLI mode): the run still completes via best-judgment.
    result = await app.runs.execute(run)
    assert result.status == RunStatus.COMPLETED
    second_body = httpx_mock.get_requests()[1].content.decode()
    assert "best judgment" in second_body
