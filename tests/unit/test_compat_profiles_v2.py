"""Compatibility profiles v2 (spec §7-§8).

The line these tests defend: a profile describes the *wire format*, and never testifies about a
model's abilities. Every capability question has to go to the evidence ledger, because a preset is
someone's reading of documentation and reads identically to a verified fact once it is stored.
"""

from __future__ import annotations

import pytest

from openagent.core.models import DiscoveryStrategy, Protocol, TransportProtocol
from openagent.providers.compat.evidence import Capability, CapabilityLedger
from openagent.providers.compat.profiles_v2 import (
    PROFILES_V2,
    AssistantHistoryPolicy,
    AuthScheme,
    CompatibilityProfile,
    ReasoningRequestStyle,
    ToolResultPolicy,
    get_profile,
)

# ------------------------------------------------------------------ protocol v2


def test_protocol_gained_the_v2_transports() -> None:
    values = {p.value for p in Protocol}
    assert {"gemini-interactions", "ollama-native-chat", "lmstudio-native-chat"} <= values


def test_existing_protocol_values_are_unchanged() -> None:
    """Provider rows persist these strings; renaming one would orphan existing connections."""

    assert Protocol.OPENAI_CHAT.value == "openai-chat"
    assert Protocol.OPENAI_RESPONSES.value == "openai-responses"
    assert Protocol.ANTHROPIC_MESSAGES.value == "anthropic-messages"


def test_transport_protocol_is_an_alias_not_a_second_enum() -> None:
    assert TransportProtocol is Protocol


def test_discovery_strategy_covers_the_v2_providers() -> None:
    values = {s.value for s in DiscoveryStrategy}
    assert {
        "openai-models",
        "gemini-models",
        "openrouter-catalog",
        "ollama-tags-show",
        "lmstudio-native",
        "curated-catalog",
        "manual-only",
    } == values


# ------------------------------------------------------------------ provider type vs transport


def test_several_providers_share_one_transport() -> None:
    """The reason adapters are keyed by transport, not by vendor."""

    chat = {n for n, p in PROFILES_V2.items() if p.transport is Protocol.OPENAI_CHAT}
    assert {"deepseek", "kimi", "glm", "minimax", "openrouter", "qwen"} <= chat


def test_local_providers_use_their_native_transports() -> None:
    assert PROFILES_V2["ollama"].transport is Protocol.OLLAMA_NATIVE_CHAT
    assert PROFILES_V2["gemini"].transport is Protocol.GEMINI_INTERACTIONS


def test_unknown_provider_falls_back_to_a_permissive_openai_shape() -> None:
    profile = get_profile("something-nobody-has-heard-of")
    assert profile.transport is Protocol.OPENAI_CHAT
    assert profile.provider_type == "generic"


# ------------------------------------------------------------------ documented deviations


def test_glm_requests_thinking_and_needs_a_tool_stream_opt_in() -> None:
    glm = get_profile("glm")
    assert glm.reasoning_request_style is ReasoningRequestStyle.THINKING_OBJECT
    assert glm.reasoning_response_field == "reasoning_content"
    assert glm.tool_stream_request_field == "tool_stream"
    assert glm.supports_tool_choice_required is False
    assert glm.supports_tool_choice_auto is True


def test_deepseek_must_replay_reasoning_with_tool_history() -> None:
    deepseek = get_profile("deepseek")
    assert deepseek.reasoning_response_field == "reasoning_content"
    assert deepseek.requires_reasoning_replay() is True


def test_minimax_replays_the_whole_assistant_message() -> None:
    minimax = get_profile("minimax")
    assert minimax.assistant_history_policy is AssistantHistoryPolicy.NATIVE_MESSAGE_WITH_REASONING
    assert minimax.requires_reasoning_replay() is True
    assert minimax.temperature_min > 0, "MiniMax rejects temperature 0"
    assert minimax.temperature_max == 1.0
    assert "n" in minimax.drop_params


def test_kimi_rejects_tool_choice_required() -> None:
    assert get_profile("kimi").supports_tool_choice_required is False


def test_gemini_and_lmstudio_declare_server_side_state() -> None:
    assert get_profile("gemini").server_state_id_field == "previous_interaction_id"
    assert get_profile("lmstudio").server_state_id_field == "previous_response_id"
    assert get_profile("gemini").tool_result_policy is ToolResultPolicy.FUNCTION_RESPONSE_PART


def test_local_providers_need_no_credential_by_default() -> None:
    assert get_profile("ollama").auth_scheme is AuthScheme.NONE
    assert get_profile("lmstudio").auth_scheme is AuthScheme.NONE


def test_gemini_uses_a_google_api_key_not_a_bearer_token() -> None:
    assert get_profile("gemini").auth_scheme is AuthScheme.GOOGLE_API_KEY


def test_nvidia_build_does_not_send_undocumented_stream_options() -> None:
    assert get_profile("nvidia-build").stream_usage is False


# ------------------------------------------------------------------ tool_choice degradation


def test_required_degrades_to_auto_where_required_is_unsupported() -> None:
    assert get_profile("kimi").normalize_tool_choice("required") == "auto"


def test_supported_tool_choice_passes_through_untouched() -> None:
    assert get_profile("openai").normalize_tool_choice("required") == "required"
    assert get_profile("openai").normalize_tool_choice("auto") == "auto"


def test_none_stays_none() -> None:
    assert get_profile("openai").normalize_tool_choice(None) is None


@pytest.mark.parametrize(
    ("provider", "value", "expected"),
    [
        ("anthropic", 1.9, 1.0),
        ("openai", 1.9, 1.9),
        ("minimax", 0.0, 0.01),
        ("minimax", 5.0, 1.0),
    ],
)
def test_temperature_is_clamped_to_the_endpoint_range(provider, value, expected) -> None:
    assert get_profile(provider).clamp_temperature(value) == pytest.approx(expected)


def test_clamping_none_returns_none_rather_than_a_default() -> None:
    """Omitting temperature and sending the minimum are different requests."""

    assert get_profile("minimax").clamp_temperature(None) is None


# ------------------------------------------------------------------ the separation itself


def test_profiles_do_not_assert_model_capabilities() -> None:
    """A profile has no way to know what a model can do, so it must not carry a claim that it does.

    `supports_parallel_tools` is the one capability-shaped field, and it stays ``None`` everywhere
    unless a request-format constraint genuinely settles it — meaning callers have to consult the
    evidence ledger rather than reading a preset as a fact.
    """

    for name, profile in PROFILES_V2.items():
        assert profile.supports_parallel_tools is None, (
            f"{name} encodes a parallel-tools claim; capability truth belongs in CapabilityEvidence"
        )


def test_a_profile_and_a_ledger_answer_different_questions() -> None:
    profile = get_profile("deepseek")
    ledger = CapabilityLedger()

    # The profile knows the wire format...
    assert profile.reasoning_response_field == "reasoning_content"
    # ...and the ledger has not been told anything about this model yet.
    assert ledger.supports(Capability.REASONING) is None


def test_every_registered_profile_names_itself_consistently() -> None:
    for name, profile in PROFILES_V2.items():
        assert profile.provider_type == name


def test_profiles_are_immutable() -> None:
    """A shared preset that one adapter can mutate becomes a cross-provider bug."""

    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        get_profile("openai").max_tokens_field = "nope"  # type: ignore[misc]


def test_profile_defaults_are_the_common_openai_shape() -> None:
    profile = CompatibilityProfile("x")
    assert profile.transport is Protocol.OPENAI_CHAT
    assert profile.max_tokens_field == "max_tokens"
    assert profile.assistant_history_policy is AssistantHistoryPolicy.NORMALIZED_TEXT
    assert profile.supports_server_state is False
