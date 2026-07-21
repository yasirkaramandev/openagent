"""Behaviour coverage for the pure helpers in ``runtimes/cli/updates`` (spec §6).

The version-parsing functions moved to ``core.versioning`` (tested there), so these exercise the
source-matched update-argv selection, the cache validity rule, the credential-free network
environment, and config load/save error handling — the branches that decide *which* update command
runs and whether a cached check is still trusted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from openagent.core.models import (
    CliInstallation,
    CliInstallSource,
    CliUpdateStatus,
)
from openagent.runtimes.cli.updates import (
    CliUpdateConfig,
    _installer_placeholder,
    _update_argv,
    cache_valid,
    load_update_config,
    save_update_config,
    update_environment,
)

pytestmark = pytest.mark.unit


def _install(**kw) -> CliInstallation:
    base = {"id": "cli_x", "type": "codex", "executable": "/usr/bin/x"}
    base.update(kw)
    return CliInstallation(**base)


def _status(**kw) -> CliUpdateStatus:
    return CliUpdateStatus(**kw)


# --------------------------------------------------------------------------- update-argv selection


def test_update_argv_npm_pins_latest() -> None:
    argv = _update_argv(_install(type="codex", install_source=CliInstallSource.NPM), _status())
    assert argv == ["npm", "install", "-g", "@openai/codex@latest"]


def test_update_argv_homebrew_cask_claude_channels() -> None:
    latest = _update_argv(
        _install(
            type="claude",
            install_source=CliInstallSource.HOMEBREW_CASK,
            release_channel="latest",
        ),
        _status(),
    )
    stable = _update_argv(
        _install(type="claude", install_source=CliInstallSource.HOMEBREW_CASK), _status()
    )
    codex = _update_argv(
        _install(type="codex", install_source=CliInstallSource.HOMEBREW_CASK), _status()
    )
    assert latest == ["brew", "upgrade", "--cask", "claude-code@latest"]
    assert stable == ["brew", "upgrade", "--cask", "claude-code"]
    assert codex == ["brew", "upgrade", "--cask", "codex"]


def test_update_argv_winget_claude_and_codex() -> None:
    claude = _update_argv(
        _install(type="claude", install_source=CliInstallSource.WINGET), _status()
    )
    codex = _update_argv(_install(type="codex", install_source=CliInstallSource.WINGET), _status())
    assert claude == ["winget", "upgrade", "--id", "Anthropic.ClaudeCode", "--exact"]
    assert codex == ["winget", "upgrade", "--id", "OpenAI.Codex", "--exact"]


def test_update_argv_native_claude_uses_self_update() -> None:
    argv = _update_argv(
        _install(type="claude", executable="/opt/claude", install_source=CliInstallSource.NATIVE),
        _status(),
    )
    assert argv == ["/opt/claude", "update"]


def test_update_argv_codex_native_methods() -> None:
    inst = _install(type="codex", executable="/opt/codex", install_source=CliInstallSource.NATIVE)
    assert _update_argv(inst, _status(update_method="codex-update")) == ["/opt/codex", "update"]
    assert _update_argv(inst, _status(update_method="codex-upgrade")) == ["/opt/codex", "--upgrade"]
    installer = _update_argv(inst, _status(update_method="codex-official-installer"))
    assert installer == _installer_placeholder("codex")


def test_update_argv_antigravity_native_uses_installer() -> None:
    argv = _update_argv(
        _install(type="antigravity", install_source=CliInstallSource.NATIVE), _status()
    )
    assert argv == _installer_placeholder("antigravity")


def test_update_argv_unmatched_source_is_none() -> None:
    # Package-manager sources with no safe non-elevated updater return None (blocked upstream).
    assert _update_argv(_install(install_source=CliInstallSource.APT), _status()) is None
    assert _update_argv(_install(install_source=CliInstallSource.UNKNOWN), _status()) is None


# --------------------------------------------------------------------------- cache validity


def test_cache_valid_rules() -> None:
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    assert cache_valid(None, now=now) is False
    assert cache_valid(_status(cache_expires_at=None), now=now) is False
    fresh = _status(cache_expires_at=now + timedelta(hours=1))
    assert cache_valid(fresh, now=now) is True
    expired = _status(cache_expires_at=now - timedelta(hours=1))
    assert cache_valid(expired, now=now) is False
    # A naive timestamp is treated as UTC rather than crashing on the comparison.
    naive = _status(cache_expires_at=(now + timedelta(hours=1)).replace(tzinfo=None))
    assert cache_valid(naive, now=now) is True


# --------------------------------------------------------------------------- network environment


def test_update_environment_carries_only_transport_settings(monkeypatch) -> None:
    parent = {
        "PATH": "/usr/bin",
        "HTTPS_PROXY": "http://proxy:8080",
        "NO_PROXY": "localhost",
        "OPENAI_API_KEY": "sk-should-not-leak",
    }
    env = update_environment(parent)
    assert env["HTTPS_PROXY"] == "http://proxy:8080"
    assert env["NO_PROXY"] == "localhost"
    assert "OPENAI_API_KEY" not in env  # provider secrets never reach the updater child


# --------------------------------------------------------------------------- config load/save


def test_load_update_config_missing_file_is_default(tmp_path: Path) -> None:
    assert load_update_config(tmp_path) == CliUpdateConfig()


def test_load_update_config_malformed_json_is_default(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("{ not json", encoding="utf-8")
    assert load_update_config(tmp_path) == CliUpdateConfig()


def test_load_update_config_non_dict_json_is_default(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert load_update_config(tmp_path) == CliUpdateConfig()


def test_save_then_load_roundtrips_and_merges(tmp_path: Path) -> None:
    # An unrelated key already in the file must survive the merge.
    (tmp_path / "config.json").write_text('{"other": {"keep": true}}', encoding="utf-8")
    cfg = CliUpdateConfig(check_interval_hours=12, check_before_run=False)
    save_update_config(tmp_path, cfg)

    import json

    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert on_disk["other"] == {"keep": True}
    assert load_update_config(tmp_path) == cfg


def test_save_over_non_dict_config_replaces_it(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("[1, 2, 3]", encoding="utf-8")
    cfg = CliUpdateConfig(check_interval_hours=8)
    save_update_config(tmp_path, cfg)
    assert load_update_config(tmp_path) == cfg
