"""CLI adapter registry + discovery (spec §32 ``openagent discover``, §41 doctor).

Also the single source of the CLI **catalog** the Add-Agent wizard renders — display name, install
state, detected executable/version, auth, capabilities, and an honest status label (spec §17) — so
the TUI never hard-codes the list of CLIs.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...core.models import CliInstallation
from .antigravity import AntigravityAdapter
from .base import CliAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter

#: Known first-class CLI adapters, keyed by type.
_BUILDERS = {
    "codex": CodexAdapter,
    "claude": ClaudeAdapter,
    "antigravity": AntigravityAdapter,
}

#: Human-readable titles for each registry key.
_DISPLAY_NAMES = {
    "codex": "Codex CLI",
    "claude": "Claude Code",
    "antigravity": "Antigravity",
}

#: Honest verification status per CLI (spec §17 vocabulary). Kept here, not in the UI, so the label
#: reflects what has actually been validated rather than "it uses a known protocol".
_STATUS_LABELS = {
    "codex": "Live schema-validated (real success turn pending on this host)",
    "claude": "Fixture validated (not yet run against a live claude CLI)",
    "antigravity": "Verified live (print+json result and resume captured)",
}


@dataclass
class CliRegistryEntry:
    """Everything the wizard/doctor needs about one CLI, resolved against the live machine."""

    type: str
    display_name: str
    executable: str | None
    version: str | None
    installed: bool
    authenticated: bool | None
    auth_detail: str
    adapter: str
    structured_events: bool
    resumable: bool
    experimental: bool
    status_label: str


def build_cli_adapter(cli_type: str, executable: str | None = None) -> CliAdapter:
    builder = _BUILDERS.get(cli_type)
    if builder is None:
        raise KeyError(f"unknown CLI type {cli_type!r}; known: {sorted(_BUILDERS)}")
    return builder(executable) if executable else builder()


def known_cli_types() -> list[str]:
    return list(_BUILDERS)


def cli_display_name(cli_type: str) -> str:
    return _DISPLAY_NAMES.get(cli_type, cli_type)


def cli_status_label(cli_type: str) -> str:
    return _STATUS_LABELS.get(cli_type, "Installed but unverified")


def cli_install_status() -> list[tuple[str, bool]]:
    """``(cli_type, installed)`` for each known CLI, using each adapter's real executable lookup.

    The install check goes through the adapter (which knows its own executable name) rather than
    assuming the display/type name is the executable name.
    """

    status: list[tuple[str, bool]] = []
    for cli_type in _BUILDERS:
        adapter = build_cli_adapter(cli_type)
        installed = getattr(adapter, "executable", None) is not None
        status.append((cli_type, installed))
    return status


async def cli_registry_entries() -> list[CliRegistryEntry]:
    """Resolve every known CLI against this machine — the catalog the Add-Agent wizard renders.

    Detection, auth inspection, and capability probing are best-effort and offline; a CLI that is
    not installed still appears (``installed=False``) so the wizard can show it as unavailable.
    """

    entries: list[CliRegistryEntry] = []
    for cli_type in _BUILDERS:
        adapter = build_cli_adapter(cli_type)
        executable = getattr(adapter, "executable", None)
        install = await adapter.detect()
        caps = await adapter.capabilities()
        authenticated: bool | None = None
        auth_detail = ""
        if install is not None:
            try:
                auth = await adapter.inspect_auth()
                authenticated, auth_detail = auth.authenticated, auth.detail
            except Exception:  # noqa: BLE001 - auth probing is best-effort/offline
                authenticated = None
        entries.append(CliRegistryEntry(
            type=cli_type,
            display_name=cli_display_name(cli_type),
            executable=executable,
            version=install.version if install else None,
            installed=install is not None,
            authenticated=authenticated,
            auth_detail=auth_detail,
            adapter=install.adapter if install else cli_type,
            structured_events=caps.structured_events,
            resumable=caps.resumable,
            experimental=caps.experimental,
            status_label=cli_status_label(cli_type),
        ))
    return entries


async def discover_installed() -> list[CliInstallation]:
    """Detect which known CLIs are installed on this machine (spec §32)."""

    found: list[CliInstallation] = []
    for cli_type in _BUILDERS:
        adapter = build_cli_adapter(cli_type)
        install = await adapter.detect()
        if install is not None:
            found.append(install)
    return found
