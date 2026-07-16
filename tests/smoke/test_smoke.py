"""Cross-platform smoke tests (item 13).

Kept OS-agnostic so they can run on macOS and Windows CI: package/CLI/TUI import and startup, path
handling, process management, and keyring fallback. No API keys, no live CLIs, no shell-specific
commands.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from openagent.app import OpenAgentApp
from openagent.config import Paths


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
    return OpenAgentApp(paths)


def test_package_and_cli_import():
    import openagent
    from openagent.cli.app import app as cli_app

    assert openagent.__version__
    assert cli_app is not None


def test_tui_imports_and_constructs(app: OpenAgentApp):
    from openagent.tui.app import OpenAgentTUI

    tui = OpenAgentTUI(app)  # constructing must not touch a real terminal
    assert tui.oa is app


async def test_tui_boots(app: OpenAgentApp):
    from openagent.tui.app import OpenAgentTUI

    tui = OpenAgentTUI(app)
    async with tui.run_test() as pilot:
        await pilot.pause()
        assert tui.screen is not None


def test_cli_help_via_runner():
    from typer.testing import CliRunner

    from openagent.cli.app import app as cli_app

    result = CliRunner().invoke(cli_app, ["--help"])
    assert result.exit_code == 0
    assert "openagent" in result.output.lower()


def test_path_handling_is_os_native(app: OpenAgentApp):
    # Run/worktree paths compose correctly on the host OS.
    run_dir = app.paths.run_dir("run_abc")
    assert run_dir.name == "run_abc"
    assert run_dir.parent == app.paths.runs_dir
    assert app.paths.worktrees_dir.is_absolute()


def test_minimal_environment_has_no_secrets(monkeypatch):
    from openagent.security.process import minimal_environment

    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    env = minimal_environment()
    assert "OPENAI_API_KEY" not in env


def test_process_run_capture_roundtrip(tmp_path: Path):
    from openagent.security.process import minimal_environment, run_capture

    # A tiny, portable child process (no shell): print a marker.
    result = run_capture(
        [sys.executable, "-c", "print('smoke-ok')"],
        cwd=tmp_path,
        env=minimal_environment(),
        timeout=30,
    )
    assert result.returncode == 0
    assert "smoke-ok" in result.stdout


def test_run_capture_timeout_terminates(tmp_path: Path):
    import subprocess

    from openagent.security.process import minimal_environment, run_capture

    with pytest.raises(subprocess.TimeoutExpired):
        run_capture(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            env=minimal_environment(),
            timeout=1,
        )


def test_keyring_fallback_does_not_crash():
    # keychain_available must return a bool on any platform, even with no backend.
    from openagent.credentials.store import keychain_available

    assert isinstance(keychain_available(), bool)


async def test_doctor_runs_offline(app: OpenAgentApp):
    checks = await app.doctor.run()
    assert checks
    assert all(c.status in {"ok", "warn", "fail"} for c in checks)
