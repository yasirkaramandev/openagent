"""CLI adapter contract + shared helpers (spec §6.2).

A CLI adapter does not build an agent loop; it runs an installed coding CLI as a subprocess and
converts its native output into OpenAgent :class:`NormalizedEvent`s. The five-method Protocol mirrors
spec §6.2.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ...core.events import NormalizedEvent
from ...core.models import CliInstallation


@dataclass
class CliRunRequest:
    run_id: str
    prompt: str
    workspace: Path
    permission_profile: str = "safe-edit"
    #: Credentials to inject only into the child environment (spec §7), e.g. {"CODEX_API_KEY": ...}.
    credential_env: dict[str, str] = field(default_factory=dict)
    session_id: str | None = None


@dataclass
class AuthStatus:
    authenticated: bool
    detail: str = ""


@dataclass
class CliCapabilities:
    structured_events: bool
    resumable: bool
    edits_files: bool
    runs_commands: bool
    experimental: bool = False


@runtime_checkable
class CliAdapter(Protocol):
    """The CLI adapter contract (spec §6.2)."""

    async def detect(self) -> CliInstallation | None: ...

    async def inspect_auth(self) -> AuthStatus: ...

    async def capabilities(self) -> CliCapabilities: ...

    def start_run(self, request: CliRunRequest) -> AsyncIterator[NormalizedEvent]: ...

    def resume_run(self, session_id: str, prompt: str, request: CliRunRequest) -> AsyncIterator[NormalizedEvent]: ...

    async def cancel(self, run_id: str) -> None: ...


def find_executable(*names: str) -> str | None:
    """Locate a CLI on PATH, including the common ``~/.local/bin`` install location."""

    for name in names:
        found = shutil.which(name)
        if found:
            return found
    # Fallback: user-local bin (codex/agy install here and may be off PATH in some shells).
    for name in names:
        candidate = Path.home() / ".local" / "bin" / name
        if candidate.exists():
            return str(candidate)
    return None


def detect_version(executable: str) -> str | None:
    try:
        result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
        out = (result.stdout or result.stderr).strip()
        return out.splitlines()[0] if out else None
    except (OSError, subprocess.TimeoutExpired):
        return None
