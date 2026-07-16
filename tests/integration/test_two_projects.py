"""Two projects, one global database (spec §3).

OpenAgent keeps one SQLite database per *user* but writes artifacts next to each *project*. Nothing
tied a run to the project it came from, so with a second project on the same machine:

* opening OpenAgent in project B and recovering orphans marked project A's **genuinely running** run
  as orphaned — B's process legitimately owns no adapter for A's run, which the recovery read as
  "unowned";
* ``output()``/``projection()`` resolved artifacts through whichever ``Paths`` the current app had,
  i.e. the directory you happened to be in — so project B looked for project A's artifacts under
  ``B/.openagent/runs/...``, which does not exist;
* run listings mixed both projects together with no way to tell them apart.

A run now records its project (``project_id``/``project_root``) and the concrete ``artifact_dir`` it
wrote to, and reads resolve through *that*, not through the ambient working directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths
from openagent.core.events import EventType, NormalizedEvent
from openagent.core.models import ProcessIdentity, Run, RunStatus, RuntimeType
from openagent.security.process import (
    capture_process_identity,
    is_pid_alive,
    terminate_process_tree,
)
from openagent.services.run_service import RunError
from openagent.storage.event_log import EventLog
from openagent.storage.projects import project_id_for

_SLEEPER = "import time; time.sleep(120)"


def _paths(tmp_path: Path, project: str, shared_data: Path) -> Paths:
    """Two projects sharing ONE global database — the real-world layout."""

    root = tmp_path / project
    root.mkdir(parents=True, exist_ok=True)
    return Paths(
        data_dir=shared_data,
        config_dir=tmp_path / "config",
        db_path=shared_data / "openagent.db",
        project_root=root,
    )


@pytest.fixture()
def two_apps(tmp_path: Path) -> tuple[OpenAgentApp, OpenAgentApp]:
    shared = tmp_path / "xdg-data"
    shared.mkdir(parents=True, exist_ok=True)
    app_a = OpenAgentApp(_paths(tmp_path, "project_a", shared))
    app_b = OpenAgentApp(_paths(tmp_path, "project_b", shared))
    for app in (app_a, app_b):
        if not app.agents.get("fake-coder"):
            app.agents.create(name="fake-coder", runtime_type=RuntimeType.CLI, cli="fake")
    return app_a, app_b


def _live_run_in(app: OpenAgentApp, pid: int) -> Run:
    """A run that is genuinely RUNNING in `app`'s project, with a live process.

    Created through the real ``create()`` so it carries the project identity and artifact_dir the
    service actually stamps — reassigning ``run.id`` afterwards would leave ``artifact_dir`` pointing
    at the original id and make the test prove nothing.
    """

    run = app.runs.create(
        agent_name="fake-coder", prompt="long task", worktree="none", confirm_in_place=True
    )
    run.status = RunStatus.RUNNING
    run.pid = pid
    run.process_identity = capture_process_identity(pid)
    if run.process_identity is None:
        run.process_identity = ProcessIdentity(
            pid=pid,
            create_time=1.0,
            executable="/missing/openagent-test",
            command_identity="0" * 64,
        )
    run.pid_started_at = run.process_identity.create_time
    app.repos.runs.upsert(run)
    run_dir = app.runs.run_dir_for(run)
    run_dir.mkdir(parents=True, exist_ok=True)
    EventLog(run_dir, index=app.repos.event_index).append(
        NormalizedEvent(run_id=run.id, type=EventType.RUN_STARTED, source="openagent", data={})
    )
    return run


def test_the_two_apps_really_share_one_database(two_apps):
    app_a, app_b = two_apps
    assert app_a.paths.db_path == app_b.paths.db_path
    assert app_a.paths.project_root != app_b.paths.project_root


def test_project_b_orphan_recovery_leaves_project_a_running(two_apps, tmp_path: Path):
    """The headline scenario (§3): B must not orphan A's live run."""

    app_a, app_b = two_apps
    proc = subprocess.Popen([sys.executable, "-c", _SLEEPER], start_new_session=True)  # noqa: S603
    try:
        run = _live_run_in(app_a, proc.pid)

        recovered = app_b.runs.recover_orphans()

        assert run.id not in recovered, (
            "project B orphaned project A's run — recovery is not project-scoped"
        )
        assert app_a.runs.get(run.id).status == RunStatus.RUNNING
        assert is_pid_alive(proc.pid)
    finally:
        identity = capture_process_identity(proc.pid)
        if identity is not None:
            terminate_process_tree(identity)


def test_project_a_still_recovers_its_own_orphans(two_apps):
    """Scoping must not disable recovery for the project that owns the run."""

    app_a, _app_b = two_apps
    run = _live_run_in(app_a, 2_000_000_000)  # a PID that does not exist

    restarted_a = OpenAgentApp(app_a.paths)
    recovered = restarted_a.runs.recover_orphans()
    assert run.id in recovered
    assert restarted_a.runs.get(run.id).status == RunStatus.ORPHANED


def test_run_records_its_project_and_artifact_dir(two_apps):
    app_a, _ = two_apps
    run = app_a.runs.create(
        agent_name="fake-coder", prompt="x", worktree="none", confirm_in_place=True
    )
    stored = app_a.runs.get(run.id)
    assert stored.project_id == project_id_for(app_a.paths.project_root)
    assert Path(stored.project_root) == app_a.paths.project_root.resolve()
    assert stored.artifact_dir, "the run must record where its artifacts live"
    assert Path(stored.artifact_dir) == app_a.paths.run_dir(run.id)


def test_run_lists_are_project_scoped_by_default(two_apps):
    app_a, app_b = two_apps
    run_a = app_a.runs.create(
        agent_name="fake-coder", prompt="a", worktree="none", confirm_in_place=True
    )
    run_b = app_b.runs.create(
        agent_name="fake-coder", prompt="b", worktree="none", confirm_in_place=True
    )

    ids_a = [r.id for r in app_a.runs.list()]
    ids_b = [r.id for r in app_b.runs.list()]
    assert run_a.id in ids_a and run_b.id not in ids_a
    assert run_b.id in ids_b and run_a.id not in ids_b


def test_a_global_list_is_available_explicitly(two_apps):
    """§3.5: a cross-project view may exist, but only when explicitly asked for."""

    app_a, app_b = two_apps
    run_a = app_a.runs.create(
        agent_name="fake-coder", prompt="a", worktree="none", confirm_in_place=True
    )
    run_b = app_b.runs.create(
        agent_name="fake-coder", prompt="b", worktree="none", confirm_in_place=True
    )
    all_ids = [r.id for r in app_a.runs.list(all_projects=True)]
    assert run_a.id in all_ids and run_b.id in all_ids


def test_artifacts_resolve_through_the_recorded_dir_not_the_current_project(two_apps):
    """Asking B for A's run must find A's artifacts — never look under B (§3.7)."""

    app_a, app_b = two_apps
    run = app_a.runs.create(
        agent_name="fake-coder", prompt="a", worktree="none", confirm_in_place=True
    )
    run_dir = app_a.paths.run_dir(run.id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "result.json").write_text(json.dumps({"status": "completed", "from": "project_a"}))

    # Cross-project output is denied unless the caller explicitly opts into the global authority.
    with pytest.raises(RunError, match="another project"):
        app_b.runs.output(run.id, "json")
    payload = json.loads(app_b.runs.output(run.id, "json", all_projects=True))
    assert payload["from"] == "project_a"


def test_projection_uses_the_recorded_artifact_dir(two_apps):
    app_a, app_b = two_apps
    run = app_a.runs.create(
        agent_name="fake-coder", prompt="a", worktree="none", confirm_in_place=True
    )
    run_dir = app_a.paths.run_dir(run.id)
    run_dir.mkdir(parents=True, exist_ok=True)
    log = EventLog(run_dir, index=app_a.repos.event_index)
    log.append(
        NormalizedEvent(run_id=run.id, type=EventType.RUN_STARTED, source="openagent", data={})
    )
    # A phase event is what actually shows up in a replay — `run.started` alone leaves the
    # projection at its defaults, so asserting on it would pass even when nothing was read.
    log.append(
        NormalizedEvent(
            run_id=run.id, type=EventType.RUN_PHASE, source="openagent", data={"phase": "running"}
        )
    )
    # Read the projection from the *other* project's app: it must actually find A's events.
    with pytest.raises(RunError, match="another project"):
        app_b.runs.projection(run.id)
    projection = app_b.runs.projection(run.id, all_projects=True)
    assert projection.run_id == run.id
    assert projection.phase == "running", (
        "the projection replayed nothing — it read the wrong project's artifact directory"
    )


async def test_cancel_uses_the_owning_projects_artifact_dir(two_apps):
    """§3.8: a cross-project cancel must write to A's run dir, not create one under B."""

    app_a, app_b = two_apps
    proc = subprocess.Popen([sys.executable, "-c", _SLEEPER], start_new_session=True)  # noqa: S603
    try:
        run = _live_run_in(app_a, proc.pid)

        assert (await app_b.runs.cancel(run.id)).value == "wrong_project"
        assert is_pid_alive(proc.pid)
        outcome = await app_b.runs.cancel(run.id, all_projects=True)
        assert outcome.value == "terminated"

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and is_pid_alive(proc.pid):
            time.sleep(0.05)
        assert not is_pid_alive(proc.pid)

        # The terminal event landed in A's run dir…
        events_a = (app_a.paths.run_dir(run.id) / "events.jsonl").read_text()
        assert "run.cancelled" in events_a
        # …and B did not fabricate a run directory of its own.
        assert not (app_b.paths.run_dir(run.id)).exists()
    finally:
        identity = capture_process_identity(proc.pid)
        if identity is not None:
            terminate_process_tree(identity)
