"""Which runs may be resumed, and who enforces it (spec §5).

Two independent defects:

1. ``resume_support()`` gated on "is the status terminal?", and ``orphaned`` **is** terminal. So an
   orphaned run with a session id reported ``True`` — including ``orphaned_unattached_process``,
   whose backend process may still be *running*. Resuming that starts a second adapter and a second
   process against the same session while the first is still alive.
2. ``resume()`` never checked the status at all. Every guard lived in ``resume_support()``, which only
   the TUI called, so any direct service call (CLI, MCP, a test, another screen) walked straight past
   it. A UI check is not a security boundary — the service must enforce its own invariants (§22).

Both now share one validation function.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.models import Run, RunStatus, RuntimeType
from openagent.security.process import (
    capture_process_identity,
    is_pid_alive,
    terminate_process_tree,
)
from openagent.services.run_service import RunError

_SLEEPER = "import time; time.sleep(120)"


@pytest.fixture()
def app(tmp_path: Path) -> OpenAgentApp:
    project = tmp_path / "proj"
    project.mkdir()
    oa = OpenAgentApp(
        Paths(
            data_dir=tmp_path / "data",
            config_dir=tmp_path / "config",
            db_path=tmp_path / "data" / "openagent.db",
            project_root=project,
        )
    )
    oa.agents.create(name="fake-coder", runtime_type=RuntimeType.CLI, cli="fake")
    return oa


def _run(app: OpenAgentApp, status: RunStatus, failure_type: str | None, **kw) -> Run:
    run = Run(
        id="run_policy",
        agent="fake-coder",
        status=status,
        failure_type=failure_type,
        provider_session_id="th-1",
        **kw,
    )
    app.repos.runs.upsert(run)
    return run


#: Every orphan reason. None of them may be resumed on the same run (§5).
ORPHAN_REASONS = [
    "orphaned_unattached_process",
    "orphaned_pid_reused",
    "orphaned_pid_unknown",
    "orphaned_pid_gone",
]


@pytest.mark.parametrize("failure_type", ORPHAN_REASONS)
def test_resume_support_refuses_every_orphan_reason(app: OpenAgentApp, failure_type: str):
    run = _run(app, RunStatus.ORPHANED, failure_type)
    ok, why = app.runs.resume_support(run)
    assert ok is False, f"resume_support allowed a resume of {failure_type}"
    assert why, "a refusal must explain itself"


@pytest.mark.parametrize("failure_type", ORPHAN_REASONS)
async def test_direct_resume_call_refuses_every_orphan_reason(app: OpenAgentApp, failure_type: str):
    """Bypassing the UI must not bypass the rule — the service enforces it itself."""

    _run(app, RunStatus.ORPHANED, failure_type)
    with pytest.raises(RunError):
        await app.runs.resume("run_policy", "keep going")


async def test_cancelled_run_is_not_resumed_by_default(app: OpenAgentApp):
    _run(app, RunStatus.CANCELLED, "user_cancelled")
    ok, why = app.runs.resume_support(app.runs.get("run_policy"))
    assert ok is False
    assert "new run" in why.lower(), "the refusal should point at the safe alternative"
    with pytest.raises(RunError):
        await app.runs.resume("run_policy", "keep going")


async def test_running_run_cannot_be_resumed_through_the_service(app: OpenAgentApp):
    """A mid-flight run is not resumable, and the service — not just the UI — says so."""

    _run(app, RunStatus.RUNNING, None)
    with pytest.raises(RunError):
        await app.runs.resume("run_policy", "keep going")


def test_completed_run_is_resumable(app: OpenAgentApp):
    run = _run(app, RunStatus.COMPLETED, None)
    ok, why = app.runs.resume_support(run)
    assert ok is True, why


def test_failed_run_is_resumable(app: OpenAgentApp):
    run = _run(app, RunStatus.FAILED, "cli_not_found")
    ok, _ = app.runs.resume_support(run)
    assert ok is True


def test_run_without_session_id_is_not_resumable(app: OpenAgentApp):
    run = Run(id="run_nosess", agent="fake-coder", status=RunStatus.COMPLETED)
    app.repos.runs.upsert(run)
    ok, why = app.runs.resume_support(run)
    assert ok is False and "session" in why.lower()


async def test_live_orphan_is_never_given_a_second_process(app: OpenAgentApp, tmp_path: Path):
    """The scenario that motivates the rule (§5), with a real live process.

    A run is orphaned while its backend process is still alive and unowned. Resuming would attach a
    *second* adapter/process to the same session while the first keeps running. It must be refused,
    and the original process must be left exactly as it was — neither killed nor duplicated.
    """

    proc = subprocess.Popen([sys.executable, "-c", _SLEEPER], start_new_session=True)  # noqa: S603
    try:
        identity = capture_process_identity(proc.pid)
        assert identity is not None
        _run(
            app,
            RunStatus.ORPHANED,
            "orphaned_unattached_process",
            pid=proc.pid,
            pid_started_at=identity.create_time,
            process_identity=identity,
        )
        before = len(app.runs._cli_adapters)  # noqa: SLF001 - asserting no adapter is registered

        with pytest.raises(RunError, match="(?i)orphan"):
            await app.runs.resume("run_policy", "carry on")

        assert len(app.runs._cli_adapters) == before, (  # noqa: SLF001
            "a refused resume must not register a second adapter"
        )
        assert is_pid_alive(proc.pid), "refusing to resume must not disturb the live process"
        assert app.runs.get("run_policy").status == RunStatus.ORPHANED
        assert app.runs.get("run_policy").turns == 1, "a refused resume must not count a turn"
    finally:
        identity = capture_process_identity(proc.pid)
        if identity is not None:
            terminate_process_tree(identity)
