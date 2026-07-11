"""CLI discovery (spec §32 ``openagent discover``)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.models import CliInstallation
from ..runtimes.cli.registry import build_cli_adapter, discover_installed, known_cli_types

if TYPE_CHECKING:
    from ..app import OpenAgentApp


class DiscoveryService:
    def __init__(self, app: OpenAgentApp) -> None:
        self.app = app
        self.repos = app.repos

    async def discover(self, persist: bool = True) -> list[CliInstallation]:
        """Detect installed CLIs and (optionally) record them."""

        found = await discover_installed()
        if persist:
            for install in found:
                install = await self._augment_auth(install)
                self.repos.clis.upsert(install)
        return found

    def list(self) -> list[CliInstallation]:
        return self.repos.clis.list()

    def known_types(self) -> list[str]:
        return known_cli_types()

    async def _augment_auth(self, install: CliInstallation) -> CliInstallation:
        adapter = build_cli_adapter(install.type, install.executable)
        try:
            status = await adapter.inspect_auth()
            return install.model_copy(update={"authenticated": status.authenticated})
        except Exception:  # noqa: BLE001 - auth probing is best-effort
            return install
