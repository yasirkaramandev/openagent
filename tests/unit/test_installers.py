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
    for contract in (
        "EXPECTED_VERSION",
        "INSTALLED_VERSION",
        "DOCTOR_EXIT",
        '2) die "verify-database"',
        '3) die "verify-migration"',
        'die "verify-path"',
        "database backup:",
    ):
        assert contract in text
    assert "sudo ln" not in text


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
        "$ExpectedVersion",
        "$doctorExit",
        'Fail "verify-database"',
        'Fail "verify-migration"',
        'Fail "verify-path"',
        "database backup:",
    ):
        assert contract in text
    lowered = text.lower()
    assert "setx " not in lowered
    assert "-verb runas" not in lowered
    assert "sudo" not in lowered


def test_batch_installer_enforces_version_path_and_database_exit_codes() -> None:
    text = (ROOT / "setup.bat").read_text()
    for contract in (
        "EXPECTED_VERSION",
        "INSTALLED_VERSION",
        "DOCTOR_EXIT",
        '"verify-database"',
        '"verify-migration"',
        '"verify-path"',
        "database backup:",
        "OA_OPENAGENT_BIN",
    ):
        assert contract in text
    assert "setx " not in text.lower()


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
    assert "v0.1.3" in str(jobs["wheel-lifecycle"])
    unix_installer = str(jobs["installer-unix"])
    for scenario in ("future schema", "broken JSON", "failed migration", "Older PATH binary"):
        assert scenario in unix_installer
