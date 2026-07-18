"""Opt-in, non-inference checks against coding CLIs installed on this machine."""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from openagent.runtimes.cli.registry import build_cli_adapter

pytestmark = [
    pytest.mark.live_cli,
    pytest.mark.skipif(
        os.environ.get("OPENAGENT_LIVE_CLI_TESTS") != "1",
        reason="set OPENAGENT_LIVE_CLI_TESTS=1 to probe installed CLIs",
    ),
]


async def _installed(cli_type: str):
    adapter = build_cli_adapter(cli_type)
    installation = await adapter.detect()
    if installation is None:
        pytest.skip(f"{cli_type} is not installed")
    return adapter, installation


@pytest.mark.parametrize("cli_type", ["codex", "claude", "antigravity"])
async def test_installed_cli_version_probe(cli_type: str) -> None:
    _adapter, installation = await _installed(cli_type)

    assert installation.executable
    assert installation.version


async def test_codex_app_server_model_list() -> None:
    adapter, _installation = await _installed("codex")

    models = await asyncio.wait_for(adapter.list_models(), timeout=30)

    assert models
    assert len(models) == len(set(models))


async def test_antigravity_account_model_list() -> None:
    adapter, _installation = await _installed("antigravity")

    models = await asyncio.wait_for(adapter.list_models(), timeout=30)

    assert models
    assert len(models) == len(set(models))


async def test_claude_doctor() -> None:
    _adapter, installation = await _installed("claude")

    completed = await asyncio.to_thread(
        subprocess.run,
        [installation.executable, "doctor"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode in {0, 1}
    assert (completed.stdout + completed.stderr).strip()
