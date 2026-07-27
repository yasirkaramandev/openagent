"""Cross-runtime session resume (spec §6.4, §6.5).

Resuming a conversation is not one operation. There are four genuinely different mechanisms, and which
one applies is a property of the runtime *and* the provider *and* what actually happened on the last
turn — a CLI session id, a provider-held interaction, a replay of provider-native material, or a replay
of OpenAgent's own normalized transcript. They fail in different ways and they preserve different
amounts of the conversation, so they are named separately rather than hidden behind one "resume".

The part this module exists for is the checking. A resume is the one operation where *almost right* is
the dangerous outcome: replaying DeepSeek's native message into MiniMax is a malformed request that
errors immediately and is therefore fine, but resuming a conversation recorded against one model into a
different model **works** — it just answers worse, and nobody can attribute the regression weeks later.

So verification distinguishes two categories and never conflates them:

* a **blocker** is something that cannot produce a correct request. It refuses.
* a **warning** is something a human may legitimately accept. It asks, and the choices are explicit:
  start a new session, migrate the history deliberately, or cancel (spec §6.4).

A changed model is a warning. A changed provider is not a decision anyone gets to make.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.errors import ErrorType
from ..core.models import Protocol
from ..providers.continuation import (
    CONTINUATION_SCHEMA_VERSION,
    ContinuationEnvelope,
    ContinuationError,
    ContinuationStrategy,
)


class ResumeMode(str, Enum):
    """How the next turn attaches to the last one (spec §6.4).

    Ordered strongest to weakest by how much of the conversation survives. The weakest is always
    available, which is what makes refusing the stronger ones affordable.
    """

    #: A CLI runtime owns the session; resume by its own session id.
    NATIVE_SESSION = "native_session"
    #: The provider holds the conversation; resume by interaction/response id.
    SERVER_INTERACTION = "server_interaction"
    #: Replay provider-native material we stored — native messages, steps, thought signatures.
    CLIENT_NATIVE_REPLAY = "client_native_replay"
    #: Rebuild from OpenAgent's normalized transcript. Always available, always the weakest.
    NORMALIZED_REPLAY = "normalized_replay"
    #: This session cannot be resumed. Saying so is a feature — the alternative is a resume
    #: affordance that silently starts a new conversation.
    UNSUPPORTED = "unsupported"

    @property
    def preserves_native_state(self) -> bool:
        return self in {
            ResumeMode.NATIVE_SESSION,
            ResumeMode.SERVER_INTERACTION,
            ResumeMode.CLIENT_NATIVE_REPLAY,
        }


#: How a continuation strategy maps onto a resume mode. One-to-one except that both native replay
#: strategies land on the same mode: the difference between a message and a step list is the wire's
#: business, not the session's.
_MODE_FOR_STRATEGY = {
    ContinuationStrategy.CLI_SESSION_ID: ResumeMode.NATIVE_SESSION,
    ContinuationStrategy.REMOTE_ID: ResumeMode.SERVER_INTERACTION,
    ContinuationStrategy.NATIVE_MESSAGE_REPLAY: ResumeMode.CLIENT_NATIVE_REPLAY,
    ContinuationStrategy.NATIVE_STEPS_REPLAY: ResumeMode.CLIENT_NATIVE_REPLAY,
    ContinuationStrategy.NORMALIZED_HISTORY: ResumeMode.NORMALIZED_REPLAY,
    ContinuationStrategy.UNSUPPORTED: ResumeMode.UNSUPPORTED,
}


def mode_for(strategy: ContinuationStrategy) -> ResumeMode:
    return _MODE_FOR_STRATEGY.get(strategy, ResumeMode.NORMALIZED_REPLAY)


class ResumeChoice(str, Enum):
    """What a user may do when verification produced warnings (spec §6.4)."""

    START_NEW = "start_new"
    MIGRATE_HISTORY = "migrate_history"
    CANCEL = "cancel"


@dataclass(frozen=True)
class ResumeContext:
    """The session as recorded, and the runtime as it is now.

    Everything is optional because a session recorded by an older build may not have all of it, and a
    missing fingerprint is "unknown" — which produces a warning, not a blocker. Treating absent
    provenance as a mismatch would make every pre-v0.2 session unresumable.
    """

    #: --- what was recorded ---
    recorded_runtime: str | None = None
    recorded_provider: str | None = None
    recorded_protocol: Protocol | None = None
    recorded_model: str | None = None
    recorded_project_fingerprint: str | None = None
    recorded_provider_fingerprint: str | None = None
    recorded_model_fingerprint: str | None = None
    recorded_cli_version: str | None = None
    recorded_session_id: str | None = None

    #: --- what is available now ---
    current_runtime: str | None = None
    current_provider: str | None = None
    current_protocol: Protocol | None = None
    current_model: str | None = None
    current_project_fingerprint: str | None = None
    current_provider_fingerprint: str | None = None
    current_model_fingerprint: str | None = None
    current_cli_version: str | None = None

    #: Whether the provider/CLI still exists in this installation at all.
    provider_exists: bool = True
    model_exists: bool = True
    runtime_exists: bool = True
    #: Whether the CLI's own session store still has the session.
    session_present: bool | None = None


@dataclass
class ResumeDecision:
    """The verdict, and everything needed to explain it."""

    mode: ResumeMode
    #: Conditions that make a correct request impossible. Non-empty means refused.
    blockers: list[str] = field(default_factory=list)
    #: Conditions a human may accept. Non-empty means the user is asked.
    warnings: list[str] = field(default_factory=list)
    #: Offered only when there are warnings and no blockers.
    choices: tuple[ResumeChoice, ...] = ()
    error_type: ErrorType | None = None

    @property
    def refused(self) -> bool:
        return bool(self.blockers)

    @property
    def needs_confirmation(self) -> bool:
        return not self.refused and bool(self.warnings)

    @property
    def approved(self) -> bool:
        """Resume may proceed without asking. Only when nothing at all was flagged."""

        return not self.refused and not self.warnings and self.mode is not ResumeMode.UNSUPPORTED

    def summary(self) -> str:
        if self.refused:
            return f"cannot resume: {self.blockers[0]}"
        if self.mode is ResumeMode.UNSUPPORTED:
            return "this session cannot be resumed"
        if self.warnings:
            return f"resume needs confirmation: {self.warnings[0]}"
        return f"resume via {self.mode.value}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "approved": self.approved,
            "refused": self.refused,
            "needs_confirmation": self.needs_confirmation,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "choices": [choice.value for choice in self.choices],
            "error_type": self.error_type.value if self.error_type else None,
        }


def verify_resume(
    envelope: ContinuationEnvelope | None,
    context: ResumeContext,
    *,
    artifact_hash_ok: bool = True,
) -> ResumeDecision:
    """Decide whether, and how, a session may be resumed (spec §6.4).

    ``envelope`` may be ``None``: a session recorded before continuation artifacts existed, or one
    whose artifact was pruned. That is a normalized replay, not a refusal — the transcript is still
    there.
    """

    if envelope is None:
        decision = ResumeDecision(mode=ResumeMode.NORMALIZED_REPLAY)
        _check_environment(decision, context)
        _check_identity(decision, context)
        if not decision.blockers and not decision.warnings:
            decision.warnings.append(
                "no provider-native continuation was recorded for this session; resuming replays the "
                "normalized transcript, which the model may respond to differently"
            )
        _finalize(decision)
        return decision

    decision = ResumeDecision(mode=mode_for(envelope.strategy))

    if envelope.strategy is ContinuationStrategy.UNSUPPORTED:
        reason = envelope.opaque_fields.get("reason") if envelope.opaque_fields else None
        decision.blockers.append(
            f"this session was recorded as unresumable: {reason or 'no reason recorded'}"
        )
        decision.error_type = ErrorType.CONTINUATION_INVALID
        return decision

    # --- the envelope's own integrity, first: nothing else matters if the material is not trustworthy.
    if envelope.schema_version > CONTINUATION_SCHEMA_VERSION:
        decision.blockers.append(
            f"the continuation was written by a newer OpenAgent (schema v{envelope.schema_version}); "
            f"this build understands v{CONTINUATION_SCHEMA_VERSION}"
        )
        decision.error_type = ErrorType.CONTINUATION_INVALID
    if not artifact_hash_ok:
        # A truncated native message does not error at the provider — it degrades the turn, which is
        # the failure that cannot be attributed later. So it is a blocker, not a warning.
        decision.blockers.append(
            "the stored continuation failed its integrity check; it may be truncated or altered"
        )
        decision.error_type = ErrorType.CONTINUATION_INVALID

    _check_environment(decision, context)
    _check_binding(decision, envelope, context)
    _check_identity(decision, context)
    _check_session(decision, envelope, context)

    if decision.blockers:
        # Normalized replay survives almost every blocker, because it needs none of the native
        # material. Offering it is what makes refusing the strong modes affordable.
        if _normalized_is_viable(decision, context):
            decision.warnings.append(
                "provider-native resume is not possible for this session; the normalized transcript "
                "can still be replayed"
            )
    _finalize(decision)
    return decision


def _check_environment(decision: ResumeDecision, context: ResumeContext) -> None:
    """Does the thing that ran this session still exist here?"""

    if not context.runtime_exists:
        decision.blockers.append(
            f"the runtime this session used ({context.recorded_runtime or 'unknown'}) is no longer "
            f"configured in this installation"
        )
        decision.error_type = decision.error_type or ErrorType.SESSION_NOT_FOUND
    if not context.provider_exists:
        decision.blockers.append(
            f"the provider this session used ({context.recorded_provider or 'unknown'}) is no longer "
            f"configured"
        )
        decision.error_type = decision.error_type or ErrorType.SESSION_NOT_FOUND
    if not context.model_exists:
        # The model being gone does not prevent a resume with a *different* model; it prevents a
        # resume with the recorded one. That is a warning with an explicit migration choice.
        decision.warnings.append(
            f"the model this session used ({context.recorded_model or 'unknown'}) is no longer "
            f"available; continuing requires choosing another one"
        )


def _check_binding(
    decision: ResumeDecision, envelope: ContinuationEnvelope, context: ResumeContext
) -> None:
    """Is the native material even addressed to this provider and protocol?"""

    if context.current_provider and envelope.provider_type != context.current_provider:
        decision.blockers.append(
            f"the continuation belongs to provider {envelope.provider_type!r}, not "
            f"{context.current_provider!r}; replaying it would be a malformed request"
        )
        decision.error_type = decision.error_type or ErrorType.CONTINUATION_INVALID
    if context.current_protocol is not None and envelope.protocol is not context.current_protocol:
        decision.blockers.append(
            f"the continuation was recorded over {envelope.protocol.value}, and this connection "
            f"speaks {context.current_protocol.value}"
        )
        decision.error_type = decision.error_type or ErrorType.PROTOCOL_MISMATCH

    if context.recorded_runtime and context.current_runtime:
        if context.recorded_runtime != context.current_runtime:
            # A CLI session id means nothing to an API provider, and vice versa.
            decision.blockers.append(
                f"this session ran on the {context.recorded_runtime} runtime and would resume on "
                f"{context.current_runtime}; native session state does not transfer between runtimes"
            )
            decision.error_type = decision.error_type or ErrorType.SESSION_NOT_FOUND


def _check_identity(decision: ResumeDecision, context: ResumeContext) -> None:
    """Has anything about the model or credential changed underneath the session?"""

    if (
        context.recorded_model
        and context.current_model
        and context.recorded_model != context.current_model
    ):
        # The one that half-works, which is why it is a warning and never silent (spec §6.4).
        decision.warnings.append(
            f"this conversation was recorded with {context.recorded_model} and would continue with "
            f"{context.current_model}; the model may respond differently to the same history"
        )

    if (
        context.recorded_model_fingerprint
        and context.current_model_fingerprint
        and context.recorded_model_fingerprint != context.current_model_fingerprint
    ):
        decision.warnings.append(
            "the model's revision has changed since this session was recorded; capabilities verified "
            "then are not evidence about it now"
        )

    if (
        context.recorded_provider_fingerprint
        and context.current_provider_fingerprint
        and context.recorded_provider_fingerprint != context.current_provider_fingerprint
    ):
        decision.warnings.append(
            "the provider credential has been rotated since this session was recorded; a "
            "provider-held conversation may no longer be visible to the new credential"
        )

    if (
        context.recorded_project_fingerprint
        and context.current_project_fingerprint
        and context.recorded_project_fingerprint != context.current_project_fingerprint
    ):
        # A CLI session id is project-scoped. Resuming it from another project would attach this run
        # to somebody else's history, which is not a thing to warn about — it is a thing to refuse.
        decision.blockers.append(
            "this session belongs to a different project; a project-scoped session cannot be "
            "resumed from here"
        )
        decision.error_type = decision.error_type or ErrorType.WORKSPACE_CONFLICT

    if (
        context.recorded_cli_version
        and context.current_cli_version
        and context.recorded_cli_version != context.current_cli_version
    ):
        decision.warnings.append(
            f"the CLI has changed from {context.recorded_cli_version} to "
            f"{context.current_cli_version} since this session was recorded; its session format may "
            f"have changed with it"
        )


def _check_session(
    decision: ResumeDecision, envelope: ContinuationEnvelope, context: ResumeContext
) -> None:
    """Does the handle the strong modes depend on still resolve?"""

    if decision.mode in {ResumeMode.NATIVE_SESSION, ResumeMode.SERVER_INTERACTION}:
        if not envelope.remote_interaction_id:
            decision.blockers.append(
                f"{decision.mode.value} needs a session id and the continuation carries none"
            )
            decision.error_type = decision.error_type or ErrorType.CONTINUATION_INVALID
        elif context.session_present is False:
            # Distinct from "no id recorded": the id exists and the owner no longer has it, which is
            # an expired session rather than a broken artifact.
            decision.blockers.append(
                "the recorded session is no longer held by the runtime or provider that owned it"
            )
            decision.error_type = decision.error_type or ErrorType.REMOTE_SESSION_EXPIRED

    if decision.mode is ResumeMode.CLIENT_NATIVE_REPLAY:
        if not envelope.native_assistant_message and not envelope.native_steps:
            decision.blockers.append(
                "native replay needs the provider's own assistant material and the continuation "
                "carries none"
            )
            decision.error_type = decision.error_type or ErrorType.CONTINUATION_INVALID


def _normalized_is_viable(decision: ResumeDecision, context: ResumeContext) -> bool:
    """Whether falling back to a transcript replay is still meaningful.

    Not when the *project* is wrong — that is a boundary, not a degradation — and not when the runtime
    or provider is gone, since there is nothing left to send the transcript to.
    """

    if not context.runtime_exists or not context.provider_exists:
        return False
    return not any("different project" in blocker for blocker in decision.blockers)


def _finalize(decision: ResumeDecision) -> None:
    """Attach the choices, and only when there is a decision for a human to make."""

    if decision.refused:
        decision.choices = (ResumeChoice.START_NEW, ResumeChoice.CANCEL)
        return
    if decision.warnings:
        decision.choices = (
            ResumeChoice.MIGRATE_HISTORY,
            ResumeChoice.START_NEW,
            ResumeChoice.CANCEL,
        )


# --------------------------------------------------------------------------- terminal contract


class TurnPhase(str, Enum):
    STARTED = "turn.started"
    COMPLETED = "turn.completed"
    FAILED = "turn.failed"
    CANCELLED = "turn.cancelled"


_TERMINAL_PHASES = {TurnPhase.COMPLETED, TurnPhase.FAILED, TurnPhase.CANCELLED}


@dataclass
class TurnOutcome:
    """The single reconciled result of one resumed turn (spec §6.5)."""

    phase: TurnPhase
    reason: str = ""
    conflict: bool = False


class TurnContract:
    """Enforce "exactly one turn.started, exactly one terminal result" (spec §6.5).

    A resumed turn is where duplicate lifecycle events actually happen: the runtime may announce a
    turn, the provider may report the session already open, and a replayed history can produce a
    second completion. Rather than trusting each producer, observations are collected and reconciled
    once — fail-closed, in the same direction the run-level contract already uses: cancelled beats
    failed beats completed, and a conflict is reported rather than resolved silently.
    """

    def __init__(self) -> None:
        self._started = 0
        self._observed: list[TurnPhase] = []
        self._reasons: dict[TurnPhase, str] = {}

    def observe_start(self) -> None:
        self._started += 1

    def observe(self, phase: TurnPhase, reason: str = "") -> None:
        if phase is TurnPhase.STARTED:
            self.observe_start()
            return
        self._observed.append(phase)
        if reason and phase not in self._reasons:
            self._reasons[phase] = reason

    @property
    def start_count(self) -> int:
        return self._started

    def reconcile(self, *, cancelled: bool = False) -> TurnOutcome:
        """The one terminal outcome, and whether the producers disagreed."""

        distinct = {phase for phase in self._observed if phase in _TERMINAL_PHASES}
        conflict = len(distinct) > 1

        if cancelled or TurnPhase.CANCELLED in distinct:
            return TurnOutcome(
                TurnPhase.CANCELLED,
                self._reasons.get(TurnPhase.CANCELLED, "the turn was cancelled"),
                conflict,
            )
        if TurnPhase.FAILED in distinct:
            return TurnOutcome(
                TurnPhase.FAILED,
                self._reasons.get(TurnPhase.FAILED, "the turn failed"),
                conflict,
            )
        if TurnPhase.COMPLETED in distinct:
            return TurnOutcome(
                TurnPhase.COMPLETED, self._reasons.get(TurnPhase.COMPLETED, ""), conflict
            )
        # No terminal observation at all. Fail-closed: a turn that never reported an outcome did not
        # succeed, and recording it as completed is how a silently truncated turn becomes durable.
        return TurnOutcome(TurnPhase.FAILED, "the turn produced no terminal result", conflict=False)

    def start_violation(self) -> str | None:
        """Whether the "exactly one start" half of the contract was broken."""

        if self._started == 0:
            return "the turn produced no turn.started event"
        if self._started > 1:
            return f"the turn produced {self._started} turn.started events; exactly one is allowed"
        return None


def resume_envelope_or_refuse(
    envelope: ContinuationEnvelope,
    *,
    provider_type: str,
    protocol: Protocol,
    model_id: str | None = None,
) -> list[str]:
    """Thin bridge to :meth:`ContinuationEnvelope.verify`, for callers that want the exception.

    Kept so the envelope stays the single authority on its own binding rules — duplicating them here
    would let the two drift, and the drift would show up as a resume that one layer allows and the
    other rejects.
    """

    try:
        return envelope.verify(provider_type=provider_type, protocol=protocol, model_id=model_id)
    except ContinuationError:
        raise
