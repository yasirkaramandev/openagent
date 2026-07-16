"""An empty provider response is a failure, not a success (spec §12).

The agent loop ended a turn with::

    if not tool_calls:      # "final text answer"
        return ApiRunOutcome(completed=True, summary=text, ...)

``text`` was never checked. So a stream that closed having produced **no text and no tool calls** —
a provider hiccup, a content filter, a model that emitted only unparseable chunks, a truncated
stream — became a *completed* run with an empty summary. The user is told the task succeeded and the
artifacts record success, while the model said nothing at all.

It also emitted ``message.completed`` with ``status: completed`` and empty text first, so the event
log corroborated the lie.

Contract (§12): non-empty final text → completed; finish_task → completed; tool calls → keep looping;
provider error → failed; empty or whitespace-only → ``empty_model_response`` failure.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import RuntimeType, enum_value


def _git(args: list[str], cwd: Path) -> None:
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
    (project / "seed.txt").write_text("seed\n")
    _git(["add", "-A"], project)
    _git(["commit", "-q", "-m", "init"], project)
    oa = OpenAgentApp(
        Paths(
            data_dir=tmp_path / "data",
            config_dir=tmp_path / "config",
            db_path=tmp_path / "data" / "openagent.db",
            project_root=project,
        )
    )
    oa.providers.add(
        name="testco", provider_type="custom", base_url="https://api.test/v1", api_key="sk-x"
    )
    oa.agents.create(
        name="a",
        runtime_type=RuntimeType.API_AGENT,
        provider="testco",
        model="m",
        permission_profile="safe-edit",
    )
    return oa


async def _run(app: OpenAgentApp):
    run = app.runs.create(agent_name="a", prompt="do the thing")
    return await app.runs.execute(run)


async def test_stream_with_no_content_and_no_tool_calls_fails(
    app: OpenAgentApp, httpx_mock: HTTPXMock
):
    """The stream opens and closes having said nothing."""

    httpx_mock.add_response(
        content=_sse({"id": "c1", "choices": [{"delta": {}}]}),
        headers={"content-type": "text/event-stream"},
    )
    result = await _run(app)
    assert enum_value(result.status) == "failed"
    assert result.failure_type == "empty_model_response"


async def test_completely_empty_stream_fails(app: OpenAgentApp, httpx_mock: HTTPXMock):
    """Not even a chunk — just [DONE]."""

    httpx_mock.add_response(
        content=b"data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
    )
    result = await _run(app)
    assert enum_value(result.status) == "failed"
    assert result.failure_type == "empty_model_response"


async def test_whitespace_only_response_fails(app: OpenAgentApp, httpx_mock: HTTPXMock):
    """Whitespace is not an answer (§12)."""

    httpx_mock.add_response(
        content=_sse({"id": "c1", "choices": [{"delta": {"content": "   \n\t  "}}]}),
        headers={"content-type": "text/event-stream"},
    )
    result = await _run(app)
    assert enum_value(result.status) == "failed"
    assert result.failure_type == "empty_model_response"


async def test_all_chunks_unparseable_fails(app: OpenAgentApp, httpx_mock: HTTPXMock):
    """Every frame is dropped by the parser, so nothing survives → must not be a success."""

    httpx_mock.add_response(
        content=b"data: {not json\n\ndata: {also not json\n\ndata: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )
    result = await _run(app)
    assert enum_value(result.status) == "failed"
    assert result.failure_type == "empty_model_response"


async def test_no_fake_completed_message_event_is_emitted(app: OpenAgentApp, httpx_mock: HTTPXMock):
    """The event log must not corroborate the lie with a `message.completed` success (§12)."""

    httpx_mock.add_response(
        content=_sse({"id": "c1", "choices": [{"delta": {}}]}),
        headers={"content-type": "text/event-stream"},
    )
    result = await _run(app)
    events = [
        json.loads(line)
        for line in (app.paths.run_dir(result.id) / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    completed_messages = [
        e
        for e in events
        if e["type"] == "message.completed" and e["data"].get("status") == "completed"
    ]
    assert not completed_messages, "an empty turn must not emit a completed message event"
    assert events[-1]["type"] == "run.failed"
    # …and the artifacts agree.
    assert json.loads(app.runs.output(result.id, "json"))["status"] == "failed"


async def test_non_empty_text_still_completes(app: OpenAgentApp, httpx_mock: HTTPXMock):
    """The positive path is unchanged: a real answer still completes."""

    httpx_mock.add_response(
        content=_sse({"id": "c1", "choices": [{"delta": {"content": "here is the answer"}}]}),
        headers={"content-type": "text/event-stream"},
    )
    result = await _run(app)
    assert enum_value(result.status) == "completed"
    assert "here is the answer" in json.loads(app.runs.output(result.id, "json"))["summary"]
