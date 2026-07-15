"""Documented commands really are accepted by the CLI (item 15).

Every ``openagent …`` invocation shown in README.md and the AI skill must resolve to a real command
path. This scans those files, extracts each command's subcommand path, and asserts the CLI accepts it
(``--help`` exits 0). It catches doc drift — a renamed or removed command left behind in the docs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openagent.cli.app import app

_ROOT = Path(__file__).resolve().parents[2]
_DOCS = [_ROOT / "README.md", _ROOT / "skills" / "openagent" / "SKILL.md", _ROOT / "skills" / "README.md"]

#: The two command *groups*; everything else is a single top-level command.
_GROUPS = {"provider", "agent"}
_LINE = re.compile(r"^\s*openagent\s+(.+)$")

runner = CliRunner()


def _command_path(rest: str) -> list[str] | None:
    """The subcommand path from the text after ``openagent`` (flags/args stripped)."""
    tokens = rest.split()
    if not tokens or tokens[0].startswith("-"):
        return None  # bare `openagent` (TUI) or `openagent --help`
    if tokens[0] in _GROUPS and len(tokens) >= 2:
        return [tokens[0], tokens[1]]
    return [tokens[0]]


def _documented_paths() -> list[tuple[str, tuple[str, ...]]]:
    seen: dict[tuple[str, ...], str] = {}
    for doc in _DOCS:
        if not doc.exists():
            continue
        for raw in doc.read_text(encoding="utf-8").splitlines():
            m = _LINE.match(raw)
            if not m:
                continue
            path = _command_path(m.group(1))
            if path is not None:
                seen.setdefault(tuple(path), str(doc.relative_to(_ROOT)))
    return [(src, p) for p, src in seen.items()]


_CASES = _documented_paths()


def test_docs_actually_reference_commands() -> None:
    assert _CASES, "no openagent commands were found in the docs — the scanner is broken"


@pytest.mark.parametrize(("source", "path"), _CASES, ids=[" ".join(p) for _, p in _CASES])
def test_documented_command_is_accepted(source: str, path: tuple[str, ...]) -> None:
    result = runner.invoke(app, [*path, "--help"])
    assert result.exit_code == 0, (
        f"{source} documents `openagent {' '.join(path)}`, which the CLI does not accept"
    )
