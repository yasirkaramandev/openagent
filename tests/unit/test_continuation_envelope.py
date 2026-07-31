"""Provider-native continuation envelopes (spec §10)."""

from __future__ import annotations

import pytest

from openagent.core.models import Protocol
from openagent.providers.continuation import (
    MAX_ENVELOPE_BYTES,
    ContinuationEnvelope,
    ContinuationError,
    ContinuationStrategy,
    unsupported,
)


def _remote(**kw):
    return ContinuationEnvelope.build(
        provider_type="gemini",
        protocol=Protocol.GEMINI_INTERACTIONS,
        strategy=ContinuationStrategy.REMOTE_ID,
        remote_interaction_id="interaction_123",
        **kw,
    )


def _native_message(**kw):
    return ContinuationEnvelope.build(
        provider_type="deepseek",
        protocol=Protocol.OPENAI_CHAT,
        strategy=ContinuationStrategy.NATIVE_MESSAGE_REPLAY,
        native_assistant_message={
            "role": "assistant",
            "content": None,
            "reasoning_content": "the user wants the weather, so I should call the tool",
            "tool_calls": [{"id": "c1", "function": {"name": "weather", "arguments": "{}"}}],
        },
        **kw,
    )


# ------------------------------------------------------------------ material must fit the strategy


def test_remote_id_strategy_requires_an_id() -> None:
    with pytest.raises(ContinuationError, match="requires a remote/session id"):
        ContinuationEnvelope.build(
            provider_type="gemini",
            protocol=Protocol.GEMINI_INTERACTIONS,
            strategy=ContinuationStrategy.REMOTE_ID,
        )


def test_native_message_strategy_requires_the_message() -> None:
    with pytest.raises(ContinuationError, match="assistant message"):
        ContinuationEnvelope.build(
            provider_type="deepseek",
            protocol=Protocol.OPENAI_CHAT,
            strategy=ContinuationStrategy.NATIVE_MESSAGE_REPLAY,
        )


def test_native_steps_strategy_requires_at_least_one_step() -> None:
    with pytest.raises(ContinuationError, match="native step"):
        ContinuationEnvelope.build(
            provider_type="gemini",
            protocol=Protocol.GEMINI_INTERACTIONS,
            strategy=ContinuationStrategy.NATIVE_STEPS_REPLAY,
            native_steps=[],
        )


def test_reasoning_is_preserved_for_tool_continuation() -> None:
    """The DeepSeek case: dropping reasoning_content degrades the next turn without erroring."""

    envelope = _native_message()
    assert envelope.native_assistant_message["reasoning_content"]
    assert envelope.native_assistant_message["tool_calls"][0]["id"] == "c1"


# ------------------------------------------------------------------ binding


def test_replaying_into_a_different_provider_is_refused() -> None:
    envelope = _native_message()
    with pytest.raises(ContinuationError, match="belongs to provider"):
        envelope.verify(provider_type="minimax", protocol=Protocol.OPENAI_CHAT)


def test_replaying_over_a_different_protocol_is_refused() -> None:
    envelope = _native_message()
    with pytest.raises(ContinuationError, match="recorded over"):
        envelope.verify(provider_type="deepseek", protocol=Protocol.ANTHROPIC_MESSAGES)


def test_matching_provider_and_protocol_verifies_clean() -> None:
    assert _native_message().verify(provider_type="deepseek", protocol=Protocol.OPENAI_CHAT) == []


def test_a_changed_model_warns_rather_than_refusing() -> None:
    """The user may legitimately want this; they must not get it silently (spec §28)."""

    envelope = _native_message(model_id="deepseek-reasoner")
    warnings = envelope.verify(
        provider_type="deepseek", protocol=Protocol.OPENAI_CHAT, model_id="deepseek-chat"
    )
    assert len(warnings) == 1
    assert "deepseek-reasoner" in warnings[0] and "deepseek-chat" in warnings[0]


def test_the_same_model_produces_no_warning() -> None:
    envelope = _native_message(model_id="deepseek-reasoner")
    assert (
        envelope.verify(
            provider_type="deepseek", protocol=Protocol.OPENAI_CHAT, model_id="deepseek-reasoner"
        )
        == []
    )


# ------------------------------------------------------------------ integrity and schema


def test_tampered_material_fails_its_integrity_check() -> None:
    envelope = _native_message()
    tampered = envelope.model_copy(
        update={"native_assistant_message": {"role": "assistant", "content": "something else"}}
    )
    with pytest.raises(ContinuationError, match="integrity check"):
        tampered.verify(provider_type="deepseek", protocol=Protocol.OPENAI_CHAT)


def test_an_envelope_from_a_newer_schema_is_refused_not_guessed_at() -> None:
    envelope = _remote().model_copy(update={"schema_version": 99})
    with pytest.raises(ContinuationError, match="newer schema"):
        envelope.verify(provider_type="gemini", protocol=Protocol.GEMINI_INTERACTIONS)


def test_oversized_material_is_rejected_at_build_time() -> None:
    """Rejected where a caller can still fall back, not mid-resume."""

    with pytest.raises(ContinuationError, match="ceiling"):
        ContinuationEnvelope.build(
            provider_type="minimax",
            protocol=Protocol.OPENAI_CHAT,
            strategy=ContinuationStrategy.NATIVE_MESSAGE_REPLAY,
            native_assistant_message={"role": "assistant", "content": "x" * MAX_ENVELOPE_BYTES},
        )


def test_size_and_hash_are_sealed_at_build_time() -> None:
    envelope = _native_message()
    assert envelope.size_bytes > 0
    assert len(envelope.content_hash) == 64


# ------------------------------------------------------------------ unsupported + redaction


def test_unsupported_is_explicit_and_carries_a_reason() -> None:
    envelope = unsupported("gemini-cli", Protocol.GEMINI_INTERACTIONS, reason="no probed resume")
    assert envelope.is_resumable is False
    assert envelope.opaque_fields["reason"] == "no probed resume"


def test_redacted_summary_omits_native_payloads_and_reasoning() -> None:
    """Doctor and the UI may show that a continuation exists, never what is inside it (spec §26)."""

    envelope = _native_message(model_id="deepseek-reasoner")
    summary = envelope.redacted()
    rendered = str(summary)
    assert "reasoning_content" not in rendered
    assert "the user wants the weather" not in rendered
    assert summary["strategy"] == "native_message_replay"
    assert summary["provider_type"] == "deepseek"
    assert summary["size_bytes"] > 0


def test_redacted_summary_reports_presence_of_a_remote_id_not_its_value() -> None:
    summary = _remote().redacted()
    assert summary["has_remote_id"] is True
    assert "interaction_123" not in str(summary)
