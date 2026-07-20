"""Credentials actually reach the CLI child, on both the first turn and on resume (spec §7).

``CliRunRequest.credential_env`` existed before v0.1.5 and every adapter forwarded it into
``minimal_environment()`` — but no code path ever populated it. The field was plumbed end to end and
always empty, so a CLI child was started with no credentials whatsoever. Runs appeared to work only
because the installed CLI re-read its own login file from disk; a user authenticating through
``ANTHROPIC_API_KEY`` had no working path at all.

These tests drive the real ``RunService`` through the real adapter registry, so what is asserted is
what production builds, not a re-implementation of it. The recording adapter captures the request it
is handed rather than inspecting internals.

Resume gets its own case because it is a genuinely different code path with a different failure
mode: it must resolve the credential *again* rather than replaying one from the run record. A run
that stored the value would both keep a secret at rest and ignore a key rotated between turns.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.events import NormalizedEvent
from openagent.core.models import RuntimeType
from openagent.credentials.redaction import active_secret_count, redact
from openagent.runtimes.cli.base import CliRunRequest
from tests.fakecli import FakeCliAdapter, install_fake_cli, write_fake_script

pytestmark = pytest.mark.security

ANTHROPIC_SECRET = "sk-ant-CANARY-delivery-8c2f"
OPENAI_SECRET = "sk-CANARY-openai-delivery-4b1d"


class RecordingCliAdapter(FakeCliAdapter):
    """A fake adapter that keeps every request it was given."""

    def __init__(self, script: Path, mode: str = "complete", resume_mode: str = "resume") -> None:
        super().__init__(script, mode=mode, resume_mode=resume_mode)
        self.requests: list[CliRunRequest] = []
        #: Secrets registered for redaction at the moment the child was about to start. Captured
        #: live because the scope is released when the turn ends, so checking afterwards proves
        #: nothing about whether output produced *during* the run would have been redacted.
        self.redaction_probe: list[str] = []

    async def _drive(self, request: CliRunRequest, mode: str) -> AsyncIterator[NormalizedEvent]:
        self.requests.append(request)
        self.redaction_probe.append(redact(f"leaking {ANTHROPIC_SECRET} here"))
        async for event in super()._drive(request, mode):
            yield event


@pytest.fixture()
def app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    paths = Paths(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "openagent.db",
        project_root=project,
    )
    oa = OpenAgentApp(paths)
    oa.agents.create(
        name="claude-agent",
        runtime_type=RuntimeType.CLI,
        cli="fake",
        permission_profile="safe-edit",
    )
    return oa


@pytest.fixture()
def recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RecordingCliAdapter:
    adapter = RecordingCliAdapter(write_fake_script(tmp_path))
    install_fake_cli(monkeypatch, adapter)
    return adapter


async def test_credential_env_is_populated_for_a_cli_run(
    app: OpenAgentApp, recorder: RecordingCliAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression in one line: this list used to be empty for every run ever started."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_SECRET)
    monkeypatch.setattr(
        "openagent.services.run_service.build_child_environment",
        lambda cli_type, environ=None: _plan_for(cli_type),
    )

    run = app.runs.create(agent_name="claude-agent", prompt="do a thing", worktree="auto")
    await app.runs.execute(run)

    assert recorder.requests, "the adapter was never driven"
    delivered = recorder.requests[0].credential_env
    assert delivered.get("ANTHROPIC_API_KEY") == ANTHROPIC_SECRET


async def test_secret_is_redacted_while_the_child_is_running(
    app: OpenAgentApp, recorder: RecordingCliAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registration must happen before the child starts, not after it finishes.

    A CLI that echoes its key back in an error message does so during the run. Registering the
    secret afterwards would redact nothing that mattered.
    """

    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_SECRET)
    monkeypatch.setattr(
        "openagent.services.run_service.build_child_environment",
        lambda cli_type, environ=None: _plan_for(cli_type),
    )

    run = app.runs.create(agent_name="claude-agent", prompt="do a thing", worktree="auto")
    await app.runs.execute(run)

    assert recorder.redaction_probe, "the adapter was never driven"
    assert ANTHROPIC_SECRET not in recorder.redaction_probe[0]


async def test_secret_scope_is_released_when_the_run_ends(
    app: OpenAgentApp, recorder: RecordingCliAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Secrets leave the registry with the turn that needed them.

    Holding them for the process lifetime would keep credentials resident long after the child
    exited, and would redact a later turn's output against a stale key.
    """

    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_SECRET)
    monkeypatch.setattr(
        "openagent.services.run_service.build_child_environment",
        lambda cli_type, environ=None: _plan_for(cli_type),
    )
    before = active_secret_count()

    run = app.runs.create(agent_name="claude-agent", prompt="do a thing", worktree="auto")
    await app.runs.execute(run)

    assert active_secret_count() == before


async def test_resume_resolves_the_credential_again(
    app: OpenAgentApp, recorder: RecordingCliAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key rotated between turns takes effect; the old one is not replayed from the run record."""

    current = {"value": ANTHROPIC_SECRET}
    monkeypatch.setattr(
        "openagent.services.run_service.build_child_environment",
        lambda cli_type, environ=None: _plan_for(cli_type, current["value"]),
    )

    run = app.runs.create(agent_name="claude-agent", prompt="first turn", worktree="auto")
    await app.runs.execute(run)
    assert recorder.requests[0].credential_env["ANTHROPIC_API_KEY"] == ANTHROPIC_SECRET

    rotated = "sk-ant-CANARY-rotated-1e7b"
    current["value"] = rotated
    resumed = app.runs.get(run.id)
    if resumed is None or not resumed.provider_session_id:
        pytest.skip("the fake adapter did not produce a resumable session")
    await app.runs.resume(run.id, "second turn")

    assert len(recorder.requests) >= 2, "resume did not drive the adapter"
    assert recorder.requests[-1].credential_env["ANTHROPIC_API_KEY"] == rotated


async def test_request_repr_does_not_leak_the_credential(
    app: OpenAgentApp, recorder: RecordingCliAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CliRunRequest reaches tracebacks and debug logs; its repr must be safe."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_SECRET)
    monkeypatch.setattr(
        "openagent.services.run_service.build_child_environment",
        lambda cli_type, environ=None: _plan_for(cli_type),
    )

    run = app.runs.create(agent_name="claude-agent", prompt="do a thing", worktree="auto")
    await app.runs.execute(run)

    assert ANTHROPIC_SECRET not in repr(recorder.requests[0])


async def test_cli_run_does_not_receive_another_services_credential(
    app: OpenAgentApp, recorder: RecordingCliAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real environment is filtered by CLI type, not passed through."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_SECRET)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "AKIA-CANARY-unrelated")

    run = app.runs.create(agent_name="claude-agent", prompt="do a thing", worktree="auto")
    await app.runs.execute(run)

    delivered = recorder.requests[0].credential_env
    # "fake" is not a CLI with a documented credential surface, so it gets nothing at all — which
    # is the correct default for an adapter OpenAgent knows nothing about.
    assert OPENAI_SECRET not in delivered.values()
    assert "AWS_SECRET_ACCESS_KEY" not in delivered


def _plan_for(cli_type: str, secret: str = ANTHROPIC_SECRET):
    """A plan standing in for a Claude-shaped CLI, so the fake adapter exercises the real path."""

    from openagent.runtimes.cli.cli_auth import ChildEnvironmentPlan, CliCredentialSource

    return ChildEnvironmentPlan(
        cli_type=cli_type,
        secret_env={"ANTHROPIC_API_KEY": secret},
        auth_source=CliCredentialSource.ENV_API_KEY,
    )
