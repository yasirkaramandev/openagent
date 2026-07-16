"""Approval flow end to end through a run (spec §29, item 8).

A destructive command an API agent requests is gated: the approval callback decides, approval events
are recorded, and the command only runs when approved. Non-interactive runs default to deny.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import RuntimeType
from openagent.security.approvals import ApprovalRequest


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _sse(*chunks: dict) -> bytes:
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


def _tool_call_chunk(name: str, args: dict) -> dict:
    return {
        "id": "c1",
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "t1",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ]
                }
            }
        ],
    }


@pytest.fixture()
def app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    _git(["init", "-q"], project)
    _git(["config", "user.email", "t@t.com"], project)
    _git(["config", "user.name", "t"], project)
    (project / "target.txt").write_text("delete me\n")
    _git(["add", "-A"], project)
    _git(["commit", "-q", "-m", "init"], project)
    paths = Paths(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db",
        project_root=project,
    )
    oa = OpenAgentApp(paths)
    oa.providers.add(
        name="testco", provider_type="custom", base_url="https://api.test/v1", api_key="sk-x"
    )
    oa.agents.create(
        name="a",
        runtime_type=RuntimeType.API_AGENT,
        provider="testco",
        model="m",
        permission_profile="development",
    )
    return oa


def _script(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        content=_sse(_tool_call_chunk("run_command", {"command": "rm -rf target.txt"})),
        headers={"content-type": "text/event-stream"},
    )
    httpx_mock.add_response(
        content=_sse({"id": "c2", "choices": [{"delta": {"content": "attempted the delete"}}]}),
        headers={"content-type": "text/event-stream"},
    )


async def test_denied_approval_blocks_command(app: OpenAgentApp, httpx_mock: HTTPXMock):
    _script(httpx_mock)
    seen: list[ApprovalRequest] = []
    run = app.runs.create(agent_name="a", prompt="clean up")
    await app.runs.execute(run, approval_callback=lambda r: (seen.append(r), False)[1])

    events = app.runs.output(run.id, "events")
    assert "approval.requested" in events and "approval.denied" in events
    assert seen and "rm -rf target.txt" in seen[0].command
    # The worktree still has the file — the destructive command did not run.
    assert (Path(run.worktree) / "target.txt").exists()


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="uses POSIX rm")
async def test_approved_command_runs(app: OpenAgentApp, httpx_mock: HTTPXMock):
    _script(httpx_mock)
    run = app.runs.create(agent_name="a", prompt="clean up")
    await app.runs.execute(run, approval_callback=lambda r: True)

    events = app.runs.output(run.id, "events")
    assert "approval.accepted" in events
    assert not (Path(run.worktree) / "target.txt").exists()  # the delete happened


async def test_non_interactive_defaults_to_deny(app: OpenAgentApp, httpx_mock: HTTPXMock):
    _script(httpx_mock)
    run = app.runs.create(agent_name="a", prompt="clean up")
    # No approval_callback → the gate's policy applies; development requires approval for destructive.
    await app.runs.execute(run)
    events = app.runs.output(run.id, "events")
    assert "approval.denied" in events
    assert (Path(run.worktree) / "target.txt").exists()
