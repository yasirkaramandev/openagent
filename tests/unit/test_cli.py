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
    add = runner.invoke(
        app,
        [
            "provider",
            "add",
            "testco",
            "--type",
            "custom",
            "--base-url",
            "https://api.test/v1",
            "--key-env",
            "TESTCO_KEY",
        ],
    )
    assert add.exit_code == 0, add.stdout
    listed = runner.invoke(app, ["provider", "list"])
    assert "testco" in listed.stdout


def test_provider_add_key_required_no_credential_rejected():
    # 'no key' for a key-required preset (deepseek) is refused at the service layer.
    result = runner.invoke(app, ["provider", "add", "ds", "--type", "deepseek", "--no-key"])
    assert result.exit_code == 1
    listed = runner.invoke(app, ["provider", "list"])
    assert "ds" not in listed.stdout


def test_provider_remove_refused_when_in_use():
    add = runner.invoke(
        app,
        [
            "provider",
            "add",
            "ds",
            "--type",
            "custom",
            "--base-url",
            "https://api.test/v1",
            "--key-env",
            "DS_KEY",
        ],
    )
    assert add.exit_code == 0, add.stdout
    agent = runner.invoke(app, ["add", "--name", "ds-coder", "--provider", "ds", "--model", "m"])
    assert agent.exit_code == 0, agent.stdout
    removed = runner.invoke(app, ["provider", "remove", "ds"])
    assert removed.exit_code == 1
    assert "ds-coder" in removed.stdout + str(removed.stderr or "")
    # Still present.
    assert "ds" in runner.invoke(app, ["provider", "list"]).stdout


def test_add_cli_agent_creates_openagent_md():
    result = runner.invoke(
        app,
        [
            "add",
            "--name",
            "codex-coder",
            "--title",
            "Codex Coder",
            "--cli",
            "codex",
            "--tag",
            "coder",
        ],
    )
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


def test_output_json_is_emitted_verbatim_not_soft_wrapped(monkeypatch):
    """`openagent output --format json` must stay valid JSON when piped (regression).

    ``console.print`` soft-wraps at the console width (80 when the output is not a TTY), injecting
    newlines mid-string — which broke the exact ``--format json`` call OPENAGENT.md tells AI
    assistants to parse. The artifact must be emitted byte-for-byte instead.
    """

    import json

    from openagent.services.run_service import RunService

    long_run = "x" * 300  # far wider than any console; any wrapping would split this run
    payload = json.dumps({"status": "completed", "summary": long_run})
    monkeypatch.setattr(
        RunService, "output", lambda self, run_id, fmt, *, all_projects=False: payload
    )

    result = runner.invoke(app, ["output", "--id", "run_x", "--format", "json"])
    assert result.exit_code == 0
    assert long_run in result.stdout  # the 300-char run survived intact — nothing wrapped it
    assert json.loads(result.stdout) == json.loads(payload)


def test_provider_presets():
    result = runner.invoke(app, ["provider", "presets"])
    assert result.exit_code == 0
    assert "deepseek" in result.stdout
    assert "anthropic" in result.stdout


def test_runs_empty():
    result = runner.invoke(app, ["runs"])
    assert result.exit_code == 0


# --------------------------------------------------------------------------- CLI agent --model (item 10)


def test_cli_agent_add_persists_model_and_reaches_run_argv():
    """`openagent add --cli … --model …` must persist the model AND have it reach the run argv.

    The top-level `add` used to drop `--model` on the CLI path entirely, so a CLI agent could never
    be created with a pinned model from the CLI — it silently inherited the CLI's global default.
    """
    import json

    from openagent.runtimes.cli.antigravity import AntigravityAdapter
    from openagent.runtimes.cli.base import CliRunRequest

    result = runner.invoke(
        app,
        [
            "add",
            "--name",
            "agy-worker",
            "--cli",
            "antigravity",
            "--model",
            "Gemini 3.5 Flash (Low)",
        ],
    )
    assert result.exit_code == 0, result.stdout

    shown = runner.invoke(app, ["agent", "show", "agy-worker"])
    assert shown.exit_code == 0, shown.stdout
    agent = json.loads(shown.stdout)
    assert agent["runtime"]["model"] == "Gemini 3.5 Flash (Low)"

    # And the persisted model is exactly what a real run would hand the CLI (RunService._run_cli
    # builds the request with ``model=agent.runtime.model or None``).
    args = AntigravityAdapter(executable="agy", allow_experimental_edit=False)._build_args(
        CliRunRequest(
            run_id="r",
            prompt="x",
            workspace=Path.cwd(),
            permission_profile="read-only",
            model=agent["runtime"]["model"] or None,
        ),
        "x",
    )
    assert args[args.index("--model") + 1] == "Gemini 3.5 Flash (Low)"


def test_agent_add_subcommand_also_persists_cli_model():
    import json

    result = runner.invoke(
        app,
        [
            "agent",
            "add",
            "--name",
            "codex-coder",
            "--cli",
            "codex",
            "--model",
            "gpt-5.5",
        ],
    )
    assert result.exit_code == 0, result.stdout
    agent = json.loads(runner.invoke(app, ["agent", "show", "codex-coder"]).stdout)
    assert agent["runtime"]["model"] == "gpt-5.5"


def test_cancel_missing_run_reports_not_found_not_false_success():
    # `openagent cancel` must never print "cancelled" for a run that does not exist (§3.3).
    result = runner.invoke(app, ["cancel", "--id", "run_nope"])
    assert result.exit_code == 1
    combined = result.stdout + str(result.stderr or "")
    assert "not found" in combined
    assert "cancelled" not in result.stdout.lower()


def test_cancel_finished_run_is_not_reported_as_cancelled():
    from openagent.app import OpenAgentApp
    from openagent.core.models import Run, RunStatus

    oa = OpenAgentApp.create()
    oa.repos.runs.upsert(Run(id="run_done", agent="x", status=RunStatus.COMPLETED))
    result = runner.invoke(app, ["cancel", "--id", "run_done"])
    assert result.exit_code == 0
    assert "already finished" in result.stdout
    assert "cancelled" not in result.stdout.lower()
