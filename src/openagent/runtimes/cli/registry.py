"""CLI adapter registry + discovery (spec §32 ``openagent discover``, §41 doctor).

Also the single source of the CLI **catalog** the Add-Agent wizard renders — display name, install
state, detected executable/version, auth, capabilities, and an honest status label (spec §17) — so
the TUI never hard-codes the list of CLIs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...core.models import CliInstallation
from .antigravity import AntigravityAdapter
from .base import CliAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter

#: Known first-class CLI adapters, keyed by type.
_BUILDERS: dict[str, Any] = {
    "codex": CodexAdapter,
    "claude": ClaudeAdapter,
    "antigravity": AntigravityAdapter,
}

#: Human-readable titles for each registry key.
_DISPLAY_NAMES: dict[str, str] = {
    "codex": "Codex CLI",
    "claude": "Claude Code",
    "antigravity": "Antigravity",
}

#: Honest verification status per CLI (spec §17 vocabulary). Kept here, not in the UI, so the label
#: reflects what has actually been validated rather than "it uses a known protocol". These are the
#: claims for the **validated version**; :func:`cli_registry_entries` downgrades the label when the
#: version actually installed differs (item 16).
_STATUS_LABELS: dict[str, str] = {
    "codex": "Verified live (reasoning, plan, commands, files, web search and resume captured)",
    "claude": "Fixture validated (not yet run against a live claude CLI)",
    "antigravity": "Verified live, read-only (editing is experimental and opt-in)",
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
    #: The version the adapter was actually validated against, and whether that's what is installed.
    validated_version: str | None = None
    version_verified: bool = False


def build_cli_adapter(cli_type: str, executable: str | None = None) -> CliAdapter:
    builder = _BUILDERS.get(cli_type)
    if builder is None:
        raise KeyError(f"unknown CLI type {cli_type!r}; known: {sorted(_BUILDERS)}")
    return builder(executable) if executable else builder()


def register_cli_adapter(
    cli_type: str, builder: Any, *, display_name: str | None = None,
    status_label: str | None = None,
) -> None:
    """Add a CLI adapter to the registry at runtime.

    The registry is the *single* place that answers "which CLIs exist and can they run" — the wizard,
    Doctor, run preflight, and the executor all resolve through it. Registering here (rather than
    swapping out an internal function) is therefore what makes an adapter genuinely usable: it goes
    through the same preflight the built-in ones do. Tests use this to install their fake CLI.
    """

    _BUILDERS[cli_type] = builder
    _DISPLAY_NAMES[cli_type] = display_name or cli_type
    _STATUS_LABELS[cli_type] = status_label or "Installed but unverified"


def unregister_cli_adapter(cli_type: str) -> None:
    _BUILDERS.pop(cli_type, None)
    _DISPLAY_NAMES.pop(cli_type, None)
    _STATUS_LABELS.pop(cli_type, None)


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
        # An adapter validated against one specific version cannot claim "verified" on another
        # (item 16): when the installed version differs, say exactly that.
        verified = install.version_verified if install else False
        label = cli_status_label(cli_type)
        if install is not None and install.validated_version and not verified:
            label = (f"Installed but current version unverified "
                     f"(validated against {install.validated_version}, "
                     f"detected {install.version or 'unknown'})")
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
            status_label=label,
            validated_version=install.validated_version if install else None,
            version_verified=verified,
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
