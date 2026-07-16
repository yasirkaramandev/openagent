from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from openagent.security.execution_backend import (
    ContainerSandboxBackend,
    ExecutionBackendError,
    detect_container_runtime,
)
from openagent.services.run_service import RunError


def test_runtime_auto_detection_prefers_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    assert detect_container_runtime() == "docker"


def test_runtime_detection_never_falls_back_to_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    with pytest.raises(ExecutionBackendError, match="requires Docker or Podman"):
        detect_container_runtime()


def test_container_requires_explicit_image_and_isolated_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    with pytest.raises(ExecutionBackendError, match="explicit local image"):
        ContainerSandboxBackend(workspace=tmp_path, image="")
    with pytest.raises(ExecutionBackendError, match="worktree=none"):
        ContainerSandboxBackend(workspace=tmp_path, image="local:test", worktree_strategy="none")


def test_missing_image_fails_without_pull_or_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    backend = ContainerSandboxBackend(workspace=tmp_path, image="missing:test")
    calls: list[list[str]] = []

    def control(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "", "not found")

    monkeypatch.setattr(backend, "_control", control)
    with pytest.raises(ExecutionBackendError, match="will not pull or build"):
        backend.validate()
    assert calls == [["image", "inspect", "missing:test"]]


def test_validation_uses_read_only_no_network_shell_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    backend = ContainerSandboxBackend(workspace=tmp_path, image="local:test")
    calls: list[list[str]] = []

    def control(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "{}", "")

    monkeypatch.setattr(backend, "_control", control)
    backend.validate()
    probe = calls[1]
    assert calls[0] == ["image", "inspect", "local:test"]
    assert "--network" in probe and "none" in probe
    assert "--read-only" in probe
    assert ["--cap-drop", "ALL"] == probe[probe.index("--cap-drop") : probe.index("--cap-drop") + 2]
    assert probe[-3:] == ["/bin/sh", "-c", "exit 0"]


def test_container_execution_uses_tmpfs_and_hard_resource_limits_without_host_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("input")
    backend = ContainerSandboxBackend(
        workspace=workspace, image="local:test", worktree_strategy="copy"
    )
    backend._validated = True  # noqa: SLF001 - isolate execution argv from validation probe
    calls: list[list[str]] = []

    def control(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[0] == "cp" and args[1].endswith(":/workspace/."):
            exported = Path(args[2])
            (exported / "input.txt").write_text("input")
            (exported / "result.txt").write_text("result")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(backend, "_control", control)
    monkeypatch.setattr(
        "openagent.security.execution_backend.run_capture",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "ok", ""),
    )

    result = backend.execute(
        ["/bin/sh", "-c", "true"],
        cwd=workspace,
        env={},
        timeout=10,
        shell=False,
        max_output_bytes=1024,
        cancellation=None,
    )

    assert result.returncode == 0
    create = next(args for args in calls if args[0] == "create")
    assert ["--network", "none"] == create[
        create.index("--network") : create.index("--network") + 2
    ]
    assert "--read-only" in create
    assert ["--cap-drop", "ALL"] == create[
        create.index("--cap-drop") : create.index("--cap-drop") + 2
    ]
    assert ["--security-opt", "no-new-privileges"] == create[
        create.index("--security-opt") : create.index("--security-opt") + 2
    ]
    for flag, value in (
        ("--cpus", "2"),
        ("--memory", "2g"),
        ("--memory-swap", "2g"),
        ("--pids-limit", "256"),
    ):
        assert [flag, value] == create[create.index(flag) : create.index(flag) + 2]
    assert create.count("--tmpfs") == 2
    assert "/workspace:rw,size=1g,mode=0700" in create
    assert "/tmp:rw,size=256m,mode=1777" in create
    assert not {"--mount", "--volume", "-v"}.intersection(create)
    assert (workspace / "result.txt").read_text() == "result"


def test_cli_run_never_silently_falls_back_from_container_to_host(paths) -> None:
    from openagent.app import OpenAgentApp
    from openagent.core.models import RuntimeType

    app = OpenAgentApp(paths)
    app.agents.create(name="cli-agent", runtime_type=RuntimeType.CLI, cli="codex")
    with pytest.raises(RunError, match="refused rather than falling back"):
        app.runs.create(
            agent_name="cli-agent",
            prompt="test",
            execution_backend="container-sandbox",
            container_image="local:test",
        )
