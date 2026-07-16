from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_unix_installer_has_valid_shell_syntax_and_managed_python_contract() -> None:
    script = ROOT / "setup.sh"
    subprocess.run(["sh", "-n", str(script)], check=True)
    text = script.read_text()
    assert "uv" in text and "python install 3.12" in text
    assert "tool install --force" in text
    assert "OPENAGENT_SETUP_NO_LAUNCH" in text


def test_powershell_installer_is_non_admin_idempotent_and_verifies_fresh_shells() -> None:
    text = (ROOT / "setup.ps1").read_text()
    for contract in (
        "python install 3.12",
        "tool install --force",
        'SetEnvironmentVariable("Path"',
        'cmd.exe /d /c "openagent version"',
        "powershell.exe -NoProfile",
        "OPENAGENT_SETUP_NO_LAUNCH",
        "Docker/Podman is absent",
        "Git is not installed",
    ):
        assert contract in text
    lowered = text.lower()
    assert "setx " not in lowered
    assert "-verb runas" not in lowered
    assert "sudo" not in lowered


def test_ci_declares_real_container_wheel_lifecycle_and_all_installer_jobs() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    jobs = workflow["jobs"]
    assert {"container-sandbox", "wheel-lifecycle", "installer-unix", "installer-windows"} <= set(
        jobs
    )
    assert "installer-powershell" in jobs
    assert "actions/setup-python" not in str(jobs["installer-powershell"])
    assert "OPENAGENT_REAL_CONTAINER" in str(jobs["container-sandbox"])
    assert "v0.1.2" in str(jobs["wheel-lifecycle"])
