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
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db",
        project_root=project,
    )
    yield OpenAgentApp(paths)
    clear_registered_secrets()


async def test_no_secret_in_any_artifact(app: OpenAgentApp, httpx_mock: HTTPXMock):
    app.providers.add(
        name="testco", provider_type="custom", base_url="https://api.test/v1", api_key="sk-x"
    )
    register_secret(PREFIXLESS)  # a prefixless provider key active for this run
    app.agents.create(
        name="a",
        runtime_type=RuntimeType.API_AGENT,
        provider="testco",
        model="m",
        permission_profile="safe-edit",
    )

    # Turn 1: model writes a file whose contents include secrets. Turn 2: final answer echoes one.
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
                                    "id": "t1",
                                    "function": {
                                        "name": "write_file",
                                        "arguments": json.dumps(
                                            {
                                                "path": "config.py",
                                                "content": f"TOKEN='{FILE_SECRET}'\nOTHER='{PREFIXLESS}'\n",
                                            }
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ],
            },
        ),
        headers={"content-type": "text/event-stream"},
    )
    httpx_mock.add_response(
        content=_sse(
            {
                "id": "c2",
                "choices": [{"delta": {"content": f"wrote key {FILE_SECRET} and {PREFIXLESS}"}}],
            },
        ),
        headers={"content-type": "text/event-stream"},
    )

    run = app.runs.create(agent_name="a", prompt=f"store this secret: {PROMPT_SECRET}")
    await app.runs.execute(run)

    run_dir = app.paths.run_dir(run.id)
    for name in (
        "request.json",
        "status.json",
        "result.json",
        "events.jsonl",
        "logs.txt",
        "output.md",
        "handoff.md",
        "changes.diff",
    ):
        text = (run_dir / name).read_text()
        for secret in (PROMPT_SECRET, FILE_SECRET, PREFIXLESS):
            assert secret not in text, f"{secret} leaked into {name}"

    # The prompt was redacted in request.json but the run still ran.
    assert "[REDACTED]" in (run_dir / "request.json").read_text()
    # The change really happened (so the diff would have contained the secret unredacted).
    assert "config.py" in json.loads((run_dir / "result.json").read_text())["files_changed"]


async def test_cli_stderr_secret_is_redacted(
    app: OpenAgentApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A secret a CLI writes to stderr must be scrubbed from the failed run's artifacts."""
    from tests.fakecli import FakeCliAdapter, install_fake_cli, write_fake_script

    adapter = FakeCliAdapter(write_fake_script(tmp_path), mode="leak_stderr")
    install_fake_cli(monkeypatch, adapter)
    app.agents.create(name="cli-a", runtime_type=RuntimeType.CLI, cli="fake")

    run = app.runs.create(agent_name="cli-a", prompt="go", worktree="auto")
    result = await app.runs.execute(run)
    assert result.status.value == "failed"  # nonzero exit, no success event

    run_dir = app.paths.run_dir(run.id)
    for name in ("events.jsonl", "logs.txt", "result.json"):
        assert "ghp_stderrLEAK1234567890abcdefGHIJ" not in (run_dir / name).read_text()


# --------------------------------------------------------------------------- NVIDIA (§12, §19)

NVIDIA_KEY = "nvapi-THIS_IS_A_FAKE_TEST_KEY_123456"
INTERNAL_REASONING = "PRIVATE INTERNAL REASONING"

_ARTIFACTS = (
    "request.json",
    "status.json",
    "result.json",
    "events.jsonl",
    "logs.txt",
    "output.md",
    "handoff.md",
    "changes.diff",
    "timeline.md",
)


async def test_nvidia_raw_reasoning_never_reaches_any_artifact(
    app: OpenAgentApp, httpx_mock: HTTPXMock
):
    """`reasoning_content` is raw chain-of-thought: only the final answer may be stored (§12, §20.4).

    End to end through a real run — not just the adapter — so this covers events.jsonl, timeline.md,
    result.json and output.md, the places an operator or another agent would actually read.
    """

    app.providers.add(
        name="nvidia-build", provider_type="nvidia-build", api_key=NVIDIA_KEY, store_key=True
    )
    app.agents.create(
        name="nv",
        runtime_type=RuntimeType.API_AGENT,
        provider="nvidia-build",
        model="nvidia/nemotron-test",
        permission_profile="safe-edit",
    )
    httpx_mock.add_response(
        content=_sse(
            {"choices": [{"delta": {"reasoning_content": INTERNAL_REASONING, "content": None}}]},
            {"choices": [{"delta": {"content": "Safe final answer"}}]},
        ),
        headers={"content-type": "text/event-stream"},
    )

    run = app.runs.create(agent_name="nv", prompt="think hard then answer")
    result = await app.runs.execute(run)
    assert result.status.value == "completed"

    run_dir = app.paths.run_dir(run.id)
    for name in _ARTIFACTS:
        path = run_dir / name
        if not path.exists():
            continue
        text = path.read_text()
        assert INTERNAL_REASONING not in text, f"raw reasoning leaked into {name}"
        assert NVIDIA_KEY not in text, f"the NVIDIA key leaked into {name}"

    # The final answer — and only the final answer — survives.
    assert "Safe final answer" in json.loads((run_dir / "result.json").read_text())["summary"]


async def test_nvidia_key_never_reaches_any_artifact_on_auth_failure(
    app: OpenAgentApp, httpx_mock: HTTPXMock
):
    """A rejected key must not be echoed back through the error path either (§19, §20.2)."""

    app.providers.add(
        name="nvidia-build", provider_type="nvidia-build", api_key=NVIDIA_KEY, store_key=True
    )
    app.agents.create(
        name="nv",
        runtime_type=RuntimeType.API_AGENT,
        provider="nvidia-build",
        model="nvidia/nemotron-test",
        permission_profile="safe-edit",
    )
    # An unhelpful provider that echoes the key back in its error body.
    httpx_mock.add_response(
        status_code=401,
        json={"error": {"message": f"invalid key: Bearer {NVIDIA_KEY}"}},
    )

    run = app.runs.create(agent_name="nv", prompt=f"use {NVIDIA_KEY}")
    result = await app.runs.execute(run)
    assert result.status.value == "failed"

    run_dir = app.paths.run_dir(run.id)
    for name in _ARTIFACTS:
        path = run_dir / name
        if not path.exists():
            continue
        assert NVIDIA_KEY not in path.read_text(), f"the NVIDIA key leaked into {name}"
