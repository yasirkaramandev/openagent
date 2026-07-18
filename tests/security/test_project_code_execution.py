"""Project code execution must never run unattended on the host (spec §3).

``safe-edit`` hands an agent ``write_file``, ``apply_patch`` **and** ``run_tests`` at the same time.
That combination is arbitrary host code execution unless the test runner itself is gated: pytest
imports every test module and every ``conftest.py`` it collects, so an agent that can write a file
into the workspace can run whatever it likes simply by asking for its "tests" to be run. Blocking
``python -c`` does nothing about this — the interpreter is invoked by pytest, not by the agent.

These tests prove the gate with a **sentinel outside the workspace**: if the payload ever executes,
the sentinel appears, and no amount of policy prose can argue it away.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from openagent.core.permissions import (
    DEVELOPMENT,
    FULL_ACCESS,
    READ_ONLY,
    SAFE_EDIT,
    get_profile,
)
from openagent.security.approvals import ApprovalGate, ApprovalRequest
from openagent.security.execution_backend import HostRestrictedBackend
from openagent.security.project_code import (
    CONTAINER_SANDBOX,
    HOST_RESTRICTED,
    ProjectCodeExecutionDecision,
    decide_project_code_execution,
)
from openagent.tools.base import ToolContext, ToolError
from openagent.tools.exec import run_tests


@pytest.fixture()
def runner_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the interpreter's own ``bin`` visible to the minimal child environment.

    ``minimal_environment`` forwards PATH but nothing else; under a non-activated venv the child
    would not find ``pytest`` at all and the test would pass for the wrong reason.
    """

    bindir = str(Path(sys.executable).parent)
    monkeypatch.setenv("PATH", os.pathsep.join([bindir, os.environ.get("PATH", "")]))


def _payload_workspace(tmp_path: Path, *, filename: str) -> tuple[Path, Path]:
    """A workspace whose *collected* code writes a sentinel outside the workspace."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = tmp_path / "outside-owned-by-test.txt"
    payload = (
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('escaped')\n"
        "\n"
        "def test_placeholder():\n"
        "    assert True\n"
    )
    (workspace / filename).write_text(payload, encoding="utf-8")
    return workspace, sentinel


class _RecordingGate(ApprovalGate):
    """An approval gate that records every request it is asked to decide."""

    def __init__(self, *, accept: bool) -> None:
        self.requests: list[ApprovalRequest] = []
        self.events: list[tuple[str, dict]] = []
        super().__init__(
            callback=self._record,
            emit=lambda name, data: self.events.append((name, data)),
            run_id="run_p0",
        )
        self._accept = accept

    def _record(self, request: ApprovalRequest) -> bool:
        self.requests.append(request)
        return self._accept


def _ctx(workspace: Path, gate: ApprovalGate) -> ToolContext:
    return ToolContext(
        workspace_root=workspace,
        profile=get_profile(SAFE_EDIT),
        approval_gate=gate,
        run_id="run_p0",
        execution_backend=HostRestrictedBackend(),
    )


@pytest.mark.parametrize("filename", ["conftest.py", "test_host_escape.py"])
def test_safe_edit_host_tests_do_not_run_without_approval(
    runner_path: None, tmp_path: Path, filename: str
) -> None:
    """The headline P0: agent-written project code must not execute on a denied approval."""

    workspace, sentinel = _payload_workspace(tmp_path, filename=filename)
    gate = _RecordingGate(accept=False)
    ctx = _ctx(workspace, gate)

    with pytest.raises(ToolError) as excinfo:
        run_tests(ctx, ["pytest", "-q", "-p", "no:cacheprovider"])

    assert not sentinel.exists(), (
        f"{filename} executed on the host without approval — the sentinel outside the workspace "
        "was created, which is arbitrary code execution"
    )
    assert "approv" in str(excinfo.value).lower()
    # The user must actually have been asked, and the refusal recorded.
    assert gate.requests, "no approval was ever requested"
    assert [name for name, _ in gate.events if name == "approval.denied"]


def test_safe_edit_host_tests_run_after_explicit_approval(
    runner_path: None, tmp_path: Path
) -> None:
    """Approval is a real gate, not a refusal: an explicit yes still runs the tests."""

    workspace, sentinel = _payload_workspace(tmp_path, filename="conftest.py")
    gate = _RecordingGate(accept=True)
    ctx = _ctx(workspace, gate)

    run_tests(ctx, ["pytest", "-q", "-p", "no:cacheprovider"])

    assert gate.requests, "an approval must still be requested before running host project code"
    assert [name for name, _ in gate.events if name == "approval.accepted"]
    # With consent the project's code does run — that is what the user agreed to.
    assert sentinel.exists()


def test_denied_approval_starts_no_subprocess(
    runner_path: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal must be decided *before* anything is spawned, not by killing it afterwards."""

    workspace, _ = _payload_workspace(tmp_path, filename="conftest.py")
    spawned: list[object] = []

    def _explode(*args: object, **kwargs: object) -> None:
        spawned.append(args)
        raise AssertionError("a subprocess was started despite the approval being denied")

    monkeypatch.setattr("openagent.security.process.subprocess.Popen", _explode)

    with pytest.raises(ToolError):
        run_tests(_ctx(workspace, _RecordingGate(accept=False)), ["pytest", "-q"])

    assert spawned == []


# --------------------------------------------------------------------------- the decision itself


@pytest.mark.parametrize(
    ("profile_name", "backend", "expected"),
    [
        (READ_ONLY, HOST_RESTRICTED, ProjectCodeExecutionDecision.DENY),
        (READ_ONLY, CONTAINER_SANDBOX, ProjectCodeExecutionDecision.DENY),
        (SAFE_EDIT, HOST_RESTRICTED, ProjectCodeExecutionDecision.REQUIRE_APPROVAL),
        (SAFE_EDIT, None, ProjectCodeExecutionDecision.REQUIRE_APPROVAL),
        (SAFE_EDIT, CONTAINER_SANDBOX, ProjectCodeExecutionDecision.ALLOW_SANDBOXED),
        # development/full-access accept host risk explicitly — that is what choosing them means.
        (DEVELOPMENT, HOST_RESTRICTED, ProjectCodeExecutionDecision.ALLOW_SANDBOXED),
        (FULL_ACCESS, HOST_RESTRICTED, ProjectCodeExecutionDecision.ALLOW_SANDBOXED),
    ],
)
def test_project_code_decision_matrix(
    profile_name: str, backend: str | None, expected: ProjectCodeExecutionDecision
) -> None:
    policy = decide_project_code_execution(profile=get_profile(profile_name), backend=backend)
    assert policy.decision is expected


def test_unknown_backend_fails_closed() -> None:
    """An unrecognised backend is not evidence of safety."""

    policy = decide_project_code_execution(profile=get_profile(SAFE_EDIT), backend="wat")
    assert policy.decision is ProjectCodeExecutionDecision.REQUIRE_APPROVAL


def test_only_the_container_backend_claims_containment() -> None:
    """``development`` on the host runs unattended but must never be described as contained."""

    host = decide_project_code_execution(profile=get_profile(DEVELOPMENT), backend=HOST_RESTRICTED)
    contained = decide_project_code_execution(
        profile=get_profile(SAFE_EDIT), backend=CONTAINER_SANDBOX
    )
    assert host.decision is ProjectCodeExecutionDecision.ALLOW_SANDBOXED
    assert host.contained is False
    assert contained.contained is True


def test_container_sandbox_runs_tests_without_asking(tmp_path: Path) -> None:
    """Under a real sandbox the containment is the boundary, so no approval is needed."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class _FakeContainer:
        name = CONTAINER_SANDBOX

        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def validate(self) -> None:
            return

        def execute(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(list(argv))
            import subprocess

            return subprocess.CompletedProcess(list(argv), 0, "1 passed", "")

    backend = _FakeContainer()
    gate = _RecordingGate(accept=False)  # would refuse if it were ever consulted
    ctx = ToolContext(
        workspace_root=workspace,
        profile=get_profile(SAFE_EDIT),
        approval_gate=gate,
        run_id="run_p0",
        execution_backend=backend,  # type: ignore[arg-type]
    )

    result = run_tests(ctx, ["pytest", "-q"])

    assert result.ok
    assert backend.calls == [["pytest", "-q"]]
    assert gate.requests == [], "the sandbox is the boundary; no approval should be requested"


def test_approval_prompt_states_the_backend_and_the_risk(tmp_path: Path) -> None:
    """The human deciding must be told what they are agreeing to."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gate = _RecordingGate(accept=False)
    with pytest.raises(ToolError):
        run_tests(_ctx(workspace, gate), ["pytest", "-q"])

    detail = gate.requests[0].detail
    assert "pytest -q" in detail
    assert HOST_RESTRICTED in detail
    assert "not a sandbox" in detail or "no kernel-level isolation" in detail
