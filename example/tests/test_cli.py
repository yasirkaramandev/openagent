from __future__ import annotations

import json

from app.cli import main


def invoke(capsys, *arguments: str) -> tuple[int, str, str]:
    code = main(arguments)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_add_list_complete_and_summary_json(tmp_path, capsys) -> None:
    board = str(tmp_path / "board.json")

    code, stdout, stderr = invoke(
        capsys, "--db", board, "add", "Ship the example", "--priority", "high", "--json"
    )
    assert code == 0 and stderr == ""
    assert json.loads(stdout)["task"]["id"] == 1

    code, stdout, _ = invoke(capsys, "--db", board, "list", "--json")
    assert code == 0
    assert json.loads(stdout)["tasks"][0]["priority"] == "high"

    code, stdout, _ = invoke(capsys, "--db", board, "complete", "1", "--json")
    assert code == 0 and json.loads(stdout)["changed"] is True
    code, stdout, _ = invoke(capsys, "--db", board, "complete", "1", "--json")
    assert code == 0 and json.loads(stdout)["changed"] is False

    code, stdout, _ = invoke(capsys, "--db", board, "summary", "--json")
    assert code == 0
    assert json.loads(stdout) == {"completed": 1, "pending": 0, "total": 1}


def test_cli_human_output_and_invalid_id(tmp_path, capsys) -> None:
    board = str(tmp_path / "board.json")
    invoke(capsys, "--db", board, "add", "Readable output")

    code, stdout, _ = invoke(capsys, "--db", board, "list")
    assert code == 0
    assert "[ ] #1 Readable output [medium]" in stdout

    code, _, stderr = invoke(capsys, "--db", board, "complete", "not-a-number")
    assert code == 2
    assert "positive integer" in stderr
