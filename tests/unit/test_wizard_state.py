"""AddAgentWizardState transitions: clearing stale fields on change, secret handling (part 3)."""

from __future__ import annotations

from pydantic import SecretStr

from openagent.core.models import Protocol
from openagent.tui.wizard_state import AddAgentWizardState


def test_changing_backend_clears_incompatible_fields():
    s = AddAgentWizardState()
    s.set_backend("api")
    s.set_provider_type("deepseek")
    s.provider_name = "ds-main"
    s.api_key = SecretStr("sk-x")
    s.model = "deepseek-chat"
    # Switching to the CLI backend clears all the API/provider fields.
    s.set_backend("cli")
    assert s.provider_type is None
    assert s.provider_name is None
    assert s.api_key is None
    assert s.model is None


def test_changing_provider_clears_stale_connection_fields():
    s = AddAgentWizardState()
    s.set_backend("api")
    s.set_provider_type("deepseek")
    s.protocol = Protocol.OPENAI_CHAT
    s.api_key = SecretStr("sk-x")
    s.model = "deepseek-chat"
    s.set_provider_type("anthropic")  # a different provider
    assert s.protocol is None
    assert s.api_key is None
    assert s.model is None


def test_changing_cli_clears_stale_cli_fields():
    s = AddAgentWizardState()
    s.set_backend("cli")
    s.set_cli("codex", "/usr/bin/codex")
    s.set_cli("antigravity", "/usr/bin/agy")
    assert s.cli_type == "antigravity"
    assert s.cli_executable == "/usr/bin/agy"


def test_same_selection_preserves_fields():
    s = AddAgentWizardState()
    s.set_backend("api")
    s.set_provider_type("deepseek")
    s.model = "deepseek-chat"
    s.set_provider_type("deepseek")  # same provider -> keep model
    assert s.model == "deepseek-chat"


def test_switching_provider_mode_clears_connection_fields():
    s = AddAgentWizardState()
    s.set_backend("api")
    s.set_provider_mode("new")
    s.api_key = SecretStr("sk-x")
    s.model = "m"
    s.set_provider_mode("existing")
    assert s.api_key is None
    assert s.model is None


def test_clear_secret_drops_api_key():
    s = AddAgentWizardState()
    s.api_key = SecretStr("sk-secret")
    s.clear_secret()
    assert s.api_key is None


def test_api_key_is_secret_str_not_plaintext_in_repr():
    s = AddAgentWizardState()
    s.api_key = SecretStr("sk-super-secret")
    assert "sk-super-secret" not in repr(s)
    assert "sk-super-secret" not in str(s.model_dump())
    # The true value is only available via explicit get_secret_value().
    assert s.api_key.get_secret_value() == "sk-super-secret"
