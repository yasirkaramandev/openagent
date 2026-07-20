"""The version is single-sourced (item 8).

The version string lives in exactly one place — ``openagent.__version__``. ``pyproject.toml`` declares
the version ``dynamic`` and hatchling reads it from that constant at build time, so the CLI, the
installed distribution metadata, and the built wheel can never drift apart.
"""

from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

import openagent
from openagent.cli.app import app

TARGET = "0.1.6rc1"
_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_dunder_version_is_target() -> None:
    assert openagent.__version__ == TARGET


def test_installed_metadata_matches_dunder() -> None:
    """A built/installed distribution carries the same version as the source constant."""
    assert importlib.metadata.version("openagent") == openagent.__version__


def test_cli_version_reports_the_single_source() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert openagent.__version__ in result.stdout


def test_pyproject_keeps_a_single_version_source() -> None:
    """No static ``version = "…"`` may re-introduce a second source of truth in ``[project]``."""
    if not _PYPROJECT.exists():  # running against an installed wheel, not the source tree
        pytest.skip("pyproject.toml not present")
    text = _PYPROJECT.read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in text
    assert re.search(r"(?m)^\[tool\.hatch\.version\]", text)
    assert not re.search(r'(?m)^version\s*=\s*"', text), (
        "a hardcoded version re-appeared in pyproject"
    )
