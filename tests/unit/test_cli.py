from pathlib import Path

import pytest
from typer.testing import CliRunner

from openagent.cli.app import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _in_project(tmp_path: Path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "openagent" in result.stdout


def test_init():
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert "initialized" in result.stdout


def test_provider_add_with_key_env_and_list():
    add = runner.invoke(app, [
        "provider", "add", "testco", "--type", "custom",
        "--base-url", "https://api.test/v1", "--key-env", "TESTCO_KEY",
    ])
    assert add.exit_code == 0, add.stdout
    listed = runner.invoke(app, ["provider", "list"])
    assert "testco" in listed.stdout


def test_add_cli_agent_creates_openagent_md():
    result = runner.invoke(app, [
        "add", "--name", "codex-coder", "--title", "Codex Coder",
        "--cli", "codex", "--tag", "coder",
    ])
    assert result.exit_code == 0, result.stdout
    assert Path("OPENAGENT.md").exists()
    text = Path("OPENAGENT.md").read_text()
    assert "`codex-coder`" in text
    listed = runner.invoke(app, ["list", "--json"])
    assert "codex-coder" in listed.stdout


def test_api_agent_requires_existing_provider():
    result = runner.invoke(app, ["add", "--name", "x", "--provider", "ghost", "--model", "m"])
    assert result.exit_code == 1
    assert "not found" in result.stdout + str(result.stderr or "")


def test_doctor_json():
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    assert '"checks"' in result.stdout


def test_output_missing_run_errors():
    result = runner.invoke(app, ["output", "--id", "run_missing", "--format", "json"])
    assert result.exit_code == 1


def test_provider_presets():
    result = runner.invoke(app, ["provider", "presets"])
    assert result.exit_code == 0
    assert "deepseek" in result.stdout
    assert "anthropic" in result.stdout


def test_runs_empty():
    result = runner.invoke(app, ["runs"])
    assert result.exit_code == 0
