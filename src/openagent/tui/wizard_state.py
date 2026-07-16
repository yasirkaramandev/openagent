"""Add-Agent wizard state (spec §31, part 3).

A single, explicit state object the wizard reads and writes — never the partially hidden widgets of
another step. The API key is held as a :class:`SecretStr` and cleared on save/cancel; the state is
never serialized or logged. Changing an upstream selection clears the now-stale downstream fields, so
the final Create call can only ever submit fields consistent with the chosen path.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ..core.models import Protocol

BackendType = Literal["cli", "api"]
ProviderMode = Literal["existing", "new"]


class AddAgentWizardState(BaseModel):
    # extra="forbid" plus explicit setters keep the state minimal and consistent.
    model_config = ConfigDict(extra="forbid")

    backend_type: BackendType | None = None

    # CLI path
    cli_type: str | None = None
    cli_executable: str | None = None

    # API path
    provider_mode: ProviderMode | None = None
    provider_type: str | None = None
    provider_name: str | None = None
    protocol: Protocol | None = None
    base_url: str | None = None
    region: str | None = None
    workspace_id: str | None = None
    credential_source: str | None = None
    api_key: SecretStr | None = None
    key_env: str | None = None
    model: str | None = None
    model_override_reason: str | None = None

    # Common agent identity
    agent_name: str = ""
    title: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    system_prompt: str = ""
    permission_profile: str = "safe-edit"
    max_steps: int = 40

    # ------------------------------------------------------------------ transitions

    def set_backend(self, backend: BackendType) -> None:
        """Choose the backend category, clearing the *other* path's fields when it changes."""
        if self.backend_type == backend:
            return
        self.backend_type = backend
        # Switching category invalidates everything path-specific.
        self._clear_cli_fields()
        self._clear_provider_fields()

    def set_cli(self, cli_type: str, executable: str | None) -> None:
        if self.cli_type != cli_type:
            self._clear_cli_fields()
            self.model = None  # a different CLI invalidates any pinned model (item 11)
        self.cli_type = cli_type
        self.cli_executable = executable

    def set_provider_type(self, provider_type: str) -> None:
        """Pick a provider preset, clearing stale connection/credential/model fields on change."""
        if self.provider_type != provider_type:
            self._clear_provider_connection_fields()
        self.provider_type = provider_type

    def set_provider_mode(self, mode: ProviderMode) -> None:
        if self.provider_mode != mode:
            # Switching between new/existing invalidates connection-specific credential + model.
            self._clear_provider_connection_fields()
        self.provider_mode = mode

    def set_existing_provider(self, name: str) -> None:
        if self.provider_name != name:
            self.model = None
        self.provider_name = name

    def clear_secret(self) -> None:
        """Drop the in-memory API key (called on save/cancel)."""
        self.api_key = None

    # ------------------------------------------------------------------ internal

    def _clear_cli_fields(self) -> None:
        self.cli_type = None
        self.cli_executable = None

    def _clear_provider_fields(self) -> None:
        self.provider_mode = None
        self.provider_type = None
        self._clear_provider_connection_fields()

    def _clear_provider_connection_fields(self) -> None:
        self.provider_name = None
        self.protocol = None
        self.base_url = None
        self.region = None
        self.workspace_id = None
        self.credential_source = None
        self.api_key = None
        self.key_env = None
        self.model = None
        self.model_override_reason = None
