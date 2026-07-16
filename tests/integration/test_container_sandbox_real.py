from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from openagent.core.events import ToolCall
from openagent.core.permissions import get_profile
from openagent.security.approvals import ApprovalGate
from openagent.security.execution_backend import ContainerSandboxBackend
from openagent.tools.base import ToolContext
from openagent.tools.registry import ToolExecutor


@pytest.mark.container
def test_real_container_has_no_network_host_mount_and_syncs_safe_files(tmp_path: Path) -> None:
    """Opt-in real-engine test; CI supplies a local image and never asks the backend to pull it."""

    if os.environ.get("OPENAGENT_REAL_CONTAINER") != "1":
        pytest.skip("real Docker/Podman test requires OPENAGENT_REAL_CONTAINER=1")
    runtime = os.environ.get("OPENAGENT_CONTAINER_RUNTIME", "docker")
    image = os.environ.get("OPENAGENT_CONTAINER_IMAGE", "openagent-sandbox-ci")
    if shutil.which(runtime) is None:
        pytest.skip(f"requested container runtime {runtime!r} is unavailable")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("host snapshot\n")
    backend = ContainerSandboxBackend(
        workspace=workspace,
        image=image,
        runtime=runtime,
        worktree_strategy="copy",
    )

    executor = ToolExecutor(
        ToolContext(
            workspace_root=workspace,
            profile=get_profile("safe-edit"),
            approval_gate=ApprovalGate(auto_approve=False),
            run_id="real-container-test",
            execution_backend=backend,
        )
    )
    result = executor.execute(
        ToolCall(id="container-test", name="run_tests", arguments={"argv": ["pytest", "-q"]})
    )

    assert result.ok, result.content
    assert (workspace / "result.txt").read_text() == "container result\n"
    assert (workspace / "input.txt").read_text() == "host snapshot\n"
