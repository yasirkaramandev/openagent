"""Documented commands really are accepted by the CLI (item 15, spec §23).

Every ``openagent …`` invocation shown in README.md and the AI skill must resolve to a real command
path **and** use flags the CLI actually accepts. This scans those files, joins backslash-continued
lines, and checks both. It catches doc drift in either direction — a renamed command, or a flag that
was documented but never implemented (an AI following the skill would simply fail).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from openagent.cli.app import app

_ROOT = Path(__file__).resolve().parents[2]
_DOCS = [
    _ROOT / "README.md",
    _ROOT / "skills" / "openagent" / "SKILL.md",
    _ROOT / "skills" / "README.md",
]

#: Command groups; everything else is a single top-level command.
_GROUPS = {"provider", "agent", "cli"}
_LINE = re.compile(r"^\s*openagent\s+(.+)$")
_FLAG = re.compile(r"(?<![\w`-])--[a-z][a-z0-9-]*")

runner = CliRunner()


def _joined_lines(text: str) -> list[str]:
    """Join shell backslash continuations so a multi-line command is scanned as one command."""

    out: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buffer += line[:-1].rstrip() + " "
            continue
        out.append((buffer + line).strip() if buffer else line)
        buffer = ""
    if buffer:
        out.append(buffer.strip())
    return out


def _command_path(rest: str) -> list[str] | None:
    """The subcommand path from the text after ``openagent`` (flags/args stripped)."""
    tokens = rest.split()
    if not tokens or tokens[0].startswith("-"):
        return None  # bare `openagent` (TUI) or `openagent --help`
    if tokens[0] in _GROUPS and len(tokens) >= 2:
        return [tokens[0], tokens[1]]
    return [tokens[0]]


def _documented() -> tuple[
    list[tuple[str, tuple[str, ...]]], list[tuple[str, tuple[str, ...], str]]
]:
    """Return (command paths, flag usages) documented across the docs."""

    paths: dict[tuple[str, ...], str] = {}
    flags: dict[tuple[tuple[str, ...], str], str] = {}
    for doc in _DOCS:
        if not doc.exists():
            continue
        source = str(doc.relative_to(_ROOT))
        for line in _joined_lines(doc.read_text(encoding="utf-8")):
            m = _LINE.match(line)
            if not m:
                continue
            rest = m.group(1)
            path = _command_path(rest)
            if path is None:
                continue
            key = tuple(path)
            paths.setdefault(key, source)
            for flag in _FLAG.findall(rest):
                flags.setdefault((key, flag), source)
    return (
        [(src, p) for p, src in paths.items()],
        [(src, p, f) for (p, f), src in flags.items()],
    )


_CASES, _FLAG_CASES = _documented()


def test_docs_actually_reference_commands() -> None:
    assert _CASES, "no openagent commands were found in the docs — the scanner is broken"


@pytest.mark.parametrize(("source", "path"), _CASES, ids=[" ".join(p) for _, p in _CASES])
def test_documented_command_is_accepted(source: str, path: tuple[str, ...]) -> None:
    result = runner.invoke(app, [*path, "--help"])
    assert result.exit_code == 0, (
        f"{source} documents `openagent {' '.join(path)}`, which the CLI does not accept"
    )


def _declared_flags(path: tuple[str, ...]) -> set[str]:
    """Every long option the command actually declares, from Click's own parameter list.

    Deliberately NOT scraped from ``--help`` output: Rich renders help into an ANSI-styled, bordered
    panel and wraps/truncates it to the terminal width, so a name like ``--allow-unverified-model``
    simply is not present as a substring at 80 columns. Introspecting the command is exact and
    independent of terminal width, colour, and Rich's formatting.
    """

    command = get_command(app)
    for token in path:
        subcommands = getattr(command, "commands", None)
        if not subcommands or token not in subcommands:
            return set()
        command = subcommands[token]
    # Click adds --help to every command implicitly, so it is not in ``params``.
    flags: set[str] = {"--help"}
    for param in getattr(command, "params", []):
        for opt in (*param.opts, *getattr(param, "secondary_opts", ())):
            if opt.startswith("--"):
                flags.add(opt)
    return flags


@pytest.mark.parametrize(
    ("source", "path", "flag"),
    _FLAG_CASES,
    ids=[f"{' '.join(p)} {f}" for _, p, f in _FLAG_CASES],
)
def test_documented_flag_exists(source: str, path: tuple[str, ...], flag: str) -> None:
    declared = _declared_flags(path)
    assert declared, f"could not introspect `openagent {' '.join(path)}`"
    assert flag in declared, (
        f"{source} documents `openagent {' '.join(path)} {flag}`, but the CLI declares only "
        f"{sorted(declared)}"
    )
