"""CLI adapter registry + discovery (spec §32 ``openagent discover``, §41 doctor)."""

from __future__ import annotations

from ...core.models import CliInstallation
from .base import CliAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter

#: Known first-class CLI adapters, keyed by type.
_BUILDERS = {
    "codex": CodexAdapter,
    "claude": ClaudeAdapter,
}


def build_cli_adapter(cli_type: str, executable: str | None = None) -> CliAdapter:
    builder = _BUILDERS.get(cli_type)
    if builder is None:
        raise KeyError(f"unknown CLI type {cli_type!r}; known: {sorted(_BUILDERS)}")
    return builder(executable) if executable else builder()


def known_cli_types() -> list[str]:
    return list(_BUILDERS)


async def discover_installed() -> list[CliInstallation]:
    """Detect which known CLIs are installed on this machine (spec §32)."""

    found: list[CliInstallation] = []
    for cli_type in _BUILDERS:
        adapter = build_cli_adapter(cli_type)
        install = await adapter.detect()
        if install is not None:
            found.append(install)
    return found
