"""Cancelling an API run really stops it and records exactly one terminal event (item 9.1).

Item 9.1 through the whole RunService lifecycle: a provider that accepts the request and then goes
silent is cancelled anyway. The run ends ``cancelled``, the provider stream is ``aclose()``d, and
``events.jsonl`` carries a single terminal event — ``run.cancelled`` — as its very last entry (item
1's ordering rule must survive an API cancel too).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import RuntimeType, enum_value


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


class StallingAdapter:
    """Accepts the request, then never yields another event; records the aclose()."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.aclosed = False
        self.transport = None  # _run_api tries to aclose this in finally; None is fine

    def stream_response(self, request):
        return self._Stream(self)

    class _Stream:
        def __init__(self, parent: StallingAdapter) -> None:
            self.parent = parent

        def __aiter__(self) -> StallingAdapter._Stream:
            return self

        async def __anext__(self):
            self.parent.entered.set()
            await asyncio.Event().wait()  # block forever
            raise StopAsyncIteration  # pragma: no cover - unreachable

        async def aclose(self) -> None:
            self.parent.aclosed = True


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
    a = OpenAgentApp(Paths(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db", project_root=project,
    ))
    # A provider with a stored key so preflight passes offline (no live call).
    a.providers.add(name="testco", provider_type="custom", base_url="https://api.test/v1",
                    api_key="sk-x", store_key=True)
    a.agents.create(name="api-coder", runtime_type=RuntimeType.API_AGENT, provider="testco",
                    model="m")
    return a


async def test_stalled_api_run_cancels_with_single_terminal_event(
    app: OpenAgentApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = StallingAdapter()
    monkeypatch.setattr(app.providers, "adapter_for", lambda provider: adapter)

    run = app.runs.create(agent_name="api-coder", prompt="go", worktree="auto")
    task = asyncio.create_task(app.runs.execute(run))
    await asyncio.wait_for(adapter.entered.wait(), timeout=5)  # loop is now awaiting __anext__
    await app.runs.cancel(run.id)
    result = await asyncio.wait_for(task, timeout=10)  # must not hang

    assert enum_value(result.status) == "cancelled"
    assert adapter.aclosed, "the stalled stream must be torn down on cancel"

    events = [json.loads(line) for line in
              (app.paths.run_dir(run.id) / "events.jsonl").read_text().splitlines() if line.strip()]
    terminals = [e["type"] for e in events
                 if e["type"] in ("run.completed", "run.failed", "run.cancelled")]
    assert terminals == ["run.cancelled"], f"expected one run.cancelled, got {terminals}"
    assert events[-1]["type"] == "run.cancelled", "the terminal event must be the last log entry"
    assert json.loads(app.runs.output(run.id, "status"))["status"] == "cancelled"
