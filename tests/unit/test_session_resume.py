"""Cross-runtime resume verification and the turn terminal contract (spec §6.4, §6.5).

The failure this whole module guards against is the one that *works*: resuming a conversation recorded
against one model into a different model produces no error, just a worse answer, and nobody attributes
it weeks later. So the tests are organized around the distinction between a blocker and a warning —
what cannot produce a correct request, versus what a human may legitimately accept.
"""

from __future__ import annotations

import pytest

from openagent.core.errors import ErrorType
from openagent.core.models import Protocol
from openagent.providers.continuation import (
    CONTINUATION_SCHEMA_VERSION,
    ContinuationEnvelope,
    ContinuationStrategy,
    unsupported,
)
from openagent.services.resume import (
    ResumeChoice,
    ResumeContext,
    ResumeMode,
    TurnContract,
    TurnPhase,
    mode_for,
    verify_resume,
)


def native_envelope(
    provider: str = "deepseek", protocol: Protocol = Protocol.OPENAI_CHAT, model: str = "m1"
):
    return ContinuationEnvelope.build(
        provider_type=provider,
        protocol=protocol,
        strategy=ContinuationStrategy.NATIVE_MESSAGE_REPLAY,
        native_assistant_message={"role": "assistant", "content": "hi"},
        model_id=model,
    )


def remote_envelope(provider: str = "gemini", session: str = "int_1"):
    return ContinuationEnvelope.build(
        provider_type=provider,
        protocol=Protocol.GEMINI_INTERACTIONS,
        strategy=ContinuationStrategy.REMOTE_ID,
        remote_interaction_id=session,
        model_id="gemini-x",
    )


def cli_envelope(session: str = "sess_1"):
    return ContinuationEnvelope.build(
        provider_type="codex",
        protocol=Protocol.OPENAI_RESPONSES,
        strategy=ContinuationStrategy.CLI_SESSION_ID,
        remote_interaction_id=session,
        model_id="gpt-x",
    )


def matched(**overrides) -> ResumeContext:
    """A context where nothing has changed — the baseline an approved resume needs."""

    base = {
        "recorded_runtime": "api",
        "recorded_provider": "deepseek",
        "recorded_protocol": Protocol.OPENAI_CHAT,
        "recorded_model": "m1",
        "current_runtime": "api",
        "current_provider": "deepseek",
        "current_protocol": Protocol.OPENAI_CHAT,
        "current_model": "m1",
    }
    base.update(overrides)
    return ResumeContext(**base)  # type: ignore[arg-type]


# =========================================================================== mode mapping


class TestModeMapping:
    @pytest.mark.parametrize(
        ("strategy", "expected"),
        [
            (ContinuationStrategy.CLI_SESSION_ID, ResumeMode.NATIVE_SESSION),
            (ContinuationStrategy.REMOTE_ID, ResumeMode.SERVER_INTERACTION),
            (ContinuationStrategy.NATIVE_MESSAGE_REPLAY, ResumeMode.CLIENT_NATIVE_REPLAY),
            (ContinuationStrategy.NATIVE_STEPS_REPLAY, ResumeMode.CLIENT_NATIVE_REPLAY),
            (ContinuationStrategy.NORMALIZED_HISTORY, ResumeMode.NORMALIZED_REPLAY),
            (ContinuationStrategy.UNSUPPORTED, ResumeMode.UNSUPPORTED),
        ],
    )
    def test_each_strategy_maps_to_one_mode(self, strategy, expected) -> None:
        assert mode_for(strategy) is expected

    def test_only_the_weakest_mode_loses_native_state(self) -> None:
        assert ResumeMode.NORMALIZED_REPLAY.preserves_native_state is False
        for mode in (
            ResumeMode.NATIVE_SESSION,
            ResumeMode.SERVER_INTERACTION,
            ResumeMode.CLIENT_NATIVE_REPLAY,
        ):
            assert mode.preserves_native_state is True


# =========================================================================== the happy path


class TestApprovedResume:
    def test_an_unchanged_session_resumes_without_asking(self) -> None:
        decision = verify_resume(native_envelope(), matched())
        assert decision.approved is True
        assert decision.mode is ResumeMode.CLIENT_NATIVE_REPLAY
        assert decision.warnings == []
        assert decision.choices == ()

    def test_a_server_held_session_resumes_by_id(self) -> None:
        decision = verify_resume(
            remote_envelope(),
            matched(
                recorded_provider="gemini",
                current_provider="gemini",
                recorded_protocol=Protocol.GEMINI_INTERACTIONS,
                current_protocol=Protocol.GEMINI_INTERACTIONS,
                recorded_model="gemini-x",
                current_model="gemini-x",
                session_present=True,
            ),
        )
        assert decision.approved is True
        assert decision.mode is ResumeMode.SERVER_INTERACTION


# =========================================================================== blockers


class TestBlockers:
    """Things that cannot produce a correct request. These refuse."""

    def test_a_different_provider_is_refused_not_warned(self) -> None:
        """Not a decision anyone gets to make: the request would be malformed."""

        decision = verify_resume(
            native_envelope(provider="deepseek"), matched(current_provider="glm")
        )
        assert decision.refused is True
        assert any("deepseek" in blocker for blocker in decision.blockers)
        assert decision.error_type is ErrorType.CONTINUATION_INVALID

    def test_a_different_protocol_is_refused(self) -> None:
        decision = verify_resume(
            native_envelope(), matched(current_protocol=Protocol.ANTHROPIC_MESSAGES)
        )
        assert decision.refused is True
        assert decision.error_type in {
            ErrorType.PROTOCOL_MISMATCH,
            ErrorType.CONTINUATION_INVALID,
        }

    def test_crossing_runtimes_is_refused(self) -> None:
        """A CLI session id means nothing to an API provider, and the reverse."""

        decision = verify_resume(
            cli_envelope(),
            ResumeContext(
                recorded_runtime="cli",
                current_runtime="api",
                recorded_provider="codex",
                current_provider="codex",
                recorded_protocol=Protocol.OPENAI_RESPONSES,
                current_protocol=Protocol.OPENAI_RESPONSES,
            ),
        )
        assert decision.refused is True
        assert any("runtime" in blocker for blocker in decision.blockers)

    def test_a_failed_integrity_check_is_a_blocker_not_a_warning(self) -> None:
        """A truncated native message degrades the turn silently; that is why it refuses."""

        decision = verify_resume(native_envelope(), matched(), artifact_hash_ok=False)
        assert decision.refused is True
        assert any("integrity" in blocker for blocker in decision.blockers)

    def test_a_newer_schema_is_refused_rather_than_guessed_at(self) -> None:
        envelope = native_envelope().model_copy(
            update={"schema_version": CONTINUATION_SCHEMA_VERSION + 5}
        )
        decision = verify_resume(envelope, matched())
        assert decision.refused is True
        assert any("newer OpenAgent" in blocker for blocker in decision.blockers)

    def test_a_session_recorded_as_unresumable_carries_its_reason(self) -> None:
        envelope = unsupported(
            "gemini", Protocol.GEMINI_INTERACTIONS, reason="no headless contract"
        )
        decision = verify_resume(envelope, matched(current_provider="gemini"))
        assert decision.refused is True
        assert any("no headless contract" in blocker for blocker in decision.blockers)

    def test_a_different_project_is_refused_because_it_is_a_boundary(self) -> None:
        """A project-scoped session resumed elsewhere would attach the run to someone else's history."""

        decision = verify_resume(
            cli_envelope(),
            matched(
                recorded_provider="codex",
                current_provider="codex",
                recorded_protocol=Protocol.OPENAI_RESPONSES,
                current_protocol=Protocol.OPENAI_RESPONSES,
                recorded_project_fingerprint="proj-a",
                current_project_fingerprint="proj-b",
                session_present=True,
            ),
        )
        assert decision.refused is True
        assert decision.error_type is ErrorType.WORKSPACE_CONFLICT

    def test_a_missing_runtime_is_refused(self) -> None:
        decision = verify_resume(native_envelope(), matched(runtime_exists=False))
        assert decision.refused is True

    def test_a_remote_id_the_owner_no_longer_holds_is_an_expired_session(self) -> None:
        decision = verify_resume(
            remote_envelope(),
            matched(
                recorded_provider="gemini",
                current_provider="gemini",
                recorded_protocol=Protocol.GEMINI_INTERACTIONS,
                current_protocol=Protocol.GEMINI_INTERACTIONS,
                session_present=False,
            ),
        )
        assert decision.refused is True
        assert decision.error_type is ErrorType.REMOTE_SESSION_EXPIRED

    def test_native_replay_without_native_material_is_refused(self) -> None:
        envelope = native_envelope().model_copy(update={"native_assistant_message": None})
        decision = verify_resume(envelope, matched())
        assert decision.refused is True

    def test_a_refusal_still_offers_a_way_forward(self) -> None:
        """A refusal with no choices is a dead end; the user can always start fresh."""

        decision = verify_resume(native_envelope(), matched(current_provider="glm"))
        assert ResumeChoice.START_NEW in decision.choices
        assert ResumeChoice.CANCEL in decision.choices
        assert ResumeChoice.MIGRATE_HISTORY not in decision.choices


# =========================================================================== warnings


class TestWarnings:
    """Things a human may accept. These ask, and never proceed silently."""

    def test_a_changed_model_warns_and_offers_the_three_choices(self) -> None:
        decision = verify_resume(native_envelope(model="m1"), matched(current_model="m2"))
        assert decision.refused is False
        assert decision.needs_confirmation is True
        assert decision.approved is False
        assert any("m1" in w and "m2" in w for w in decision.warnings)
        assert set(decision.choices) == {
            ResumeChoice.MIGRATE_HISTORY,
            ResumeChoice.START_NEW,
            ResumeChoice.CANCEL,
        }

    def test_a_changed_model_revision_warns(self) -> None:
        decision = verify_resume(
            native_envelope(),
            matched(recorded_model_fingerprint="rev-1", current_model_fingerprint="rev-2"),
        )
        assert decision.needs_confirmation is True
        assert any("revision" in w for w in decision.warnings)

    def test_a_rotated_credential_warns_about_provider_held_state(self) -> None:
        decision = verify_resume(
            native_envelope(),
            matched(recorded_provider_fingerprint="cred-1", current_provider_fingerprint="cred-2"),
        )
        assert any("rotated" in w for w in decision.warnings)

    def test_a_changed_cli_version_warns(self) -> None:
        decision = verify_resume(
            cli_envelope(),
            matched(
                recorded_runtime="cli",
                current_runtime="cli",
                recorded_provider="codex",
                current_provider="codex",
                recorded_protocol=Protocol.OPENAI_RESPONSES,
                current_protocol=Protocol.OPENAI_RESPONSES,
                recorded_cli_version="0.142.5",
                current_cli_version="0.145.0",
                session_present=True,
            ),
        )
        assert any("0.145.0" in w for w in decision.warnings)

    def test_a_missing_model_warns_rather_than_refusing(self) -> None:
        """The model being gone blocks resuming with *that* model, not resuming at all."""

        decision = verify_resume(native_envelope(), matched(model_exists=False))
        assert decision.refused is False
        assert any("no longer available" in w for w in decision.warnings)

    def test_absent_provenance_is_unknown_not_a_mismatch(self) -> None:
        """Otherwise every session recorded before v0.2 becomes unresumable."""

        decision = verify_resume(
            native_envelope(),
            ResumeContext(
                current_provider="deepseek",
                current_protocol=Protocol.OPENAI_CHAT,
                current_model="m1",
            ),
        )
        assert decision.refused is False
        assert decision.approved is True


class TestNoEnvelope:
    def test_a_session_with_no_artifact_falls_back_to_transcript_replay(self) -> None:
        decision = verify_resume(None, matched())
        assert decision.mode is ResumeMode.NORMALIZED_REPLAY
        assert decision.needs_confirmation is True
        assert any("normalized transcript" in w for w in decision.warnings)

    def test_a_missing_artifact_plus_a_missing_provider_still_refuses(self) -> None:
        decision = verify_resume(None, matched(provider_exists=False))
        assert decision.refused is True


class TestFallbackOffer:
    def test_a_blocked_native_resume_offers_the_transcript(self) -> None:
        """Normalized replay needs none of the native material, so it survives most blockers."""

        envelope = native_envelope().model_copy(update={"native_assistant_message": None})
        decision = verify_resume(envelope, matched())
        assert decision.refused is True
        assert any("normalized transcript" in w for w in decision.warnings)

    def test_a_wrong_project_does_not_offer_a_transcript_fallback(self) -> None:
        """A project boundary is not a degradation to work around."""

        decision = verify_resume(
            cli_envelope(),
            matched(
                recorded_provider="codex",
                current_provider="codex",
                recorded_protocol=Protocol.OPENAI_RESPONSES,
                current_protocol=Protocol.OPENAI_RESPONSES,
                recorded_project_fingerprint="a",
                current_project_fingerprint="b",
                session_present=True,
            ),
        )
        assert not any("normalized transcript" in w for w in decision.warnings)

    def test_a_missing_provider_does_not_offer_a_fallback_either(self) -> None:
        """There is nothing left to send the transcript to."""

        decision = verify_resume(native_envelope(), matched(provider_exists=False))
        assert not any("normalized transcript" in w for w in decision.warnings)


class TestReporting:
    def test_the_decision_serializes_for_doctor_and_the_tui(self) -> None:
        payload = verify_resume(native_envelope(), matched(current_model="m2")).to_dict()
        assert payload["mode"] == "client_native_replay"
        assert payload["needs_confirmation"] is True
        assert "migrate_history" in payload["choices"]

    def test_the_summary_leads_with_the_reason(self) -> None:
        refused = verify_resume(native_envelope(), matched(current_provider="glm"))
        assert refused.summary().startswith("cannot resume")
        approved = verify_resume(native_envelope(), matched())
        assert "client_native_replay" in approved.summary()


# =========================================================================== terminal contract


class TestTurnTerminalContract:
    """Exactly one turn.started and exactly one terminal result (spec §6.5)."""

    def test_a_clean_turn(self) -> None:
        contract = TurnContract()
        contract.observe_start()
        contract.observe(TurnPhase.COMPLETED)
        outcome = contract.reconcile()
        assert outcome.phase is TurnPhase.COMPLETED
        assert outcome.conflict is False
        assert contract.start_violation() is None

    def test_completed_and_failed_together_reconciles_to_failed(self) -> None:
        contract = TurnContract()
        contract.observe_start()
        contract.observe(TurnPhase.COMPLETED)
        contract.observe(TurnPhase.FAILED, "provider rejected the replay")
        outcome = contract.reconcile()
        assert outcome.phase is TurnPhase.FAILED
        assert outcome.conflict is True
        assert "replay" in outcome.reason

    def test_completed_and_cancelled_together_reconciles_to_cancelled(self) -> None:
        contract = TurnContract()
        contract.observe_start()
        contract.observe(TurnPhase.COMPLETED)
        contract.observe(TurnPhase.CANCELLED)
        outcome = contract.reconcile()
        assert outcome.phase is TurnPhase.CANCELLED
        assert outcome.conflict is True

    def test_failed_and_cancelled_together_reconciles_to_cancelled(self) -> None:
        contract = TurnContract()
        contract.observe_start()
        contract.observe(TurnPhase.FAILED)
        contract.observe(TurnPhase.CANCELLED)
        assert contract.reconcile().phase is TurnPhase.CANCELLED

    def test_an_explicit_cancellation_outranks_a_reported_completion(self) -> None:
        contract = TurnContract()
        contract.observe_start()
        contract.observe(TurnPhase.COMPLETED)
        assert contract.reconcile(cancelled=True).phase is TurnPhase.CANCELLED

    def test_no_terminal_observation_fails_closed(self) -> None:
        """A turn that never reported an outcome did not succeed."""

        contract = TurnContract()
        contract.observe_start()
        outcome = contract.reconcile()
        assert outcome.phase is TurnPhase.FAILED
        assert "no terminal result" in outcome.reason

    def test_duplicate_completions_collapse_without_a_conflict(self) -> None:
        contract = TurnContract()
        contract.observe_start()
        contract.observe(TurnPhase.COMPLETED)
        contract.observe(TurnPhase.COMPLETED)
        outcome = contract.reconcile()
        assert outcome.phase is TurnPhase.COMPLETED
        assert outcome.conflict is False, "the same outcome twice is not a disagreement"

    def test_a_missing_start_is_reported(self) -> None:
        contract = TurnContract()
        contract.observe(TurnPhase.COMPLETED)
        assert contract.start_violation() == "the turn produced no turn.started event"

    def test_two_starts_are_reported(self) -> None:
        """A resumed turn is exactly where a duplicate announcement happens."""

        contract = TurnContract()
        contract.observe_start()
        contract.observe(TurnPhase.STARTED)
        assert contract.start_count == 2
        violation = contract.start_violation()
        assert violation is not None and "2 turn.started" in violation
