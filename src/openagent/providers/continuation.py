"""Provider-native conversation continuation (spec §10).

Resuming a conversation is not the same problem as displaying one. A normalized transcript — user
said this, assistant said that, tool returned this — is enough to *show* a session and not enough
to *continue* one, and the gap is silent. The model does not error; it produces a worse next turn,
and nobody can attribute the regression to the resume path weeks later.

What gets lost differs by provider. DeepSeek wants `reasoning_content` sent back alongside the tool
call it belongs to. MiniMax wants the assistant message preserved whole, reasoning details included.
Gemini either takes a `previous_interaction_id` or wants the thought/function-call steps replayed in
their native shape. None of that survives a round trip through normalized text.

So a continuation is stored as an envelope: a strategy that says *how* to resume, plus exactly the
native material that strategy needs, bound to the provider and protocol that produced it. The
binding is the important part — replaying DeepSeek's assistant message into MiniMax is not a
degraded resume, it is a malformed request, and the envelope refuses rather than trying.

Two standing constraints: envelopes are bounded (a conversation cannot grow one without limit) and
never contain secrets. Raw reasoning inside an envelope is not shown in the UI by default; it is
transport material, not content the user asked to read.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.models import Protocol, utcnow

#: An envelope that cannot be stored in a reasonable amount of space is not a resume mechanism.
#: At the ceiling the caller falls back to normalized replay and says so, rather than growing a
#: row until the database is the problem.
MAX_ENVELOPE_BYTES = 262_144

#: Bumped when the envelope's own shape changes. An envelope written by a newer schema is not
#: readable by an older binary, and guessing is worse than declining to resume.
CONTINUATION_SCHEMA_VERSION = 1


class ContinuationStrategy(str, Enum):
    """How the next turn attaches to the previous one (spec §10)."""

    #: The provider holds the state; send an id (Gemini `previous_interaction_id`, OpenAI
    #: `previous_response_id`).
    REMOTE_ID = "remote_id"
    #: Replay the provider's own assistant message object verbatim.
    NATIVE_MESSAGE_REPLAY = "native_message_replay"
    #: Replay a sequence of native steps (thoughts, function calls, results).
    NATIVE_STEPS_REPLAY = "native_steps_replay"
    #: Rebuild from OpenAgent's normalized transcript. Always available, always the weakest.
    NORMALIZED_HISTORY = "normalized_history"
    #: A CLI runtime owns the session; resume by its session id.
    CLI_SESSION_ID = "cli_session_id"
    #: This provider/model cannot be resumed. Saying so is a feature.
    UNSUPPORTED = "unsupported"


class ContinuationError(RuntimeError):
    """An envelope cannot be used for the requested continuation."""


class ContinuationEnvelope(BaseModel):
    """Everything needed to continue one conversation with one provider (spec §10)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = CONTINUATION_SCHEMA_VERSION
    provider_type: str
    protocol: Protocol
    strategy: ContinuationStrategy

    #: REMOTE_ID / CLI_SESSION_ID.
    remote_interaction_id: str | None = None
    #: NATIVE_MESSAGE_REPLAY — the provider's assistant message, as it sent it.
    native_assistant_message: dict[str, Any] | None = None
    #: NATIVE_STEPS_REPLAY — ordered native steps.
    native_steps: list[dict[str, Any]] = Field(default_factory=list)
    #: Provider-specific extras that must survive but that OpenAgent does not interpret.
    opaque_fields: dict[str, Any] = Field(default_factory=dict)

    #: The model this material came from. A different model may not accept it (spec §28).
    model_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    size_bytes: int = 0
    #: Integrity hash over the native material, so tampering or truncation is detectable.
    content_hash: str = ""

    # ------------------------------------------------------------------ construction

    @classmethod
    def build(
        cls,
        *,
        provider_type: str,
        protocol: Protocol,
        strategy: ContinuationStrategy,
        remote_interaction_id: str | None = None,
        native_assistant_message: dict[str, Any] | None = None,
        native_steps: list[dict[str, Any]] | None = None,
        opaque_fields: dict[str, Any] | None = None,
        model_id: str | None = None,
    ) -> ContinuationEnvelope:
        """Build a sealed envelope, or raise if the material does not fit the strategy.

        Sealing computes the size and hash up front so that an oversized envelope is rejected at
        the point it is created — where there is still a caller who can fall back — rather than at
        the point it is used, mid-resume.
        """

        envelope = cls(
            provider_type=provider_type,
            protocol=protocol,
            strategy=strategy,
            remote_interaction_id=remote_interaction_id,
            native_assistant_message=native_assistant_message,
            native_steps=list(native_steps or []),
            opaque_fields=dict(opaque_fields or {}),
            model_id=model_id,
        )
        envelope._require_material_for_strategy()
        payload = envelope._material_bytes()
        if len(payload) > MAX_ENVELOPE_BYTES:
            raise ContinuationError(
                f"continuation material is {len(payload)} bytes, over the "
                f"{MAX_ENVELOPE_BYTES}-byte ceiling; fall back to normalized history"
            )
        return envelope.model_copy(
            update={
                "size_bytes": len(payload),
                "content_hash": hashlib.sha256(payload).hexdigest(),
            }
        )

    def _material_bytes(self) -> bytes:
        material = {
            "remote_interaction_id": self.remote_interaction_id,
            "native_assistant_message": self.native_assistant_message,
            "native_steps": self.native_steps,
            "opaque_fields": self.opaque_fields,
        }
        return json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def _require_material_for_strategy(self) -> None:
        strategy = self.strategy
        if strategy in {ContinuationStrategy.REMOTE_ID, ContinuationStrategy.CLI_SESSION_ID}:
            if not self.remote_interaction_id:
                raise ContinuationError(f"{strategy.value} requires a remote/session id")
        elif strategy is ContinuationStrategy.NATIVE_MESSAGE_REPLAY:
            if not self.native_assistant_message:
                raise ContinuationError(
                    "native_message_replay requires the provider's assistant message"
                )
        elif strategy is ContinuationStrategy.NATIVE_STEPS_REPLAY:
            if not self.native_steps:
                raise ContinuationError("native_steps_replay requires at least one native step")

    # ------------------------------------------------------------------ validation

    def verify(
        self, *, provider_type: str, protocol: Protocol, model_id: str | None = None
    ) -> list[str]:
        """Check this envelope may be replayed into the given context.

        Returns warnings; raises :class:`ContinuationError` for anything that would produce a
        malformed request. The distinction matters: a changed model is a warning the user should
        see and may accept, while a changed provider is not a decision anyone gets to make.
        """

        if self.schema_version > CONTINUATION_SCHEMA_VERSION:
            raise ContinuationError(
                f"continuation was written by a newer schema (v{self.schema_version}); "
                f"this build understands v{CONTINUATION_SCHEMA_VERSION}"
            )
        if self.provider_type != provider_type:
            raise ContinuationError(
                f"continuation belongs to provider {self.provider_type!r}, not {provider_type!r}"
            )
        if self.protocol is not protocol:
            raise ContinuationError(
                f"continuation was recorded over {self.protocol.value}, not {protocol.value}"
            )
        expected = hashlib.sha256(self._material_bytes()).hexdigest()
        if self.content_hash and self.content_hash != expected:
            raise ContinuationError("continuation material failed its integrity check")

        warnings: list[str] = []
        if model_id and self.model_id and model_id != self.model_id:
            # Not fatal, but never silent: native material from one model replayed into another is
            # the kind of thing that half-works, so the user decides (spec §28).
            warnings.append(
                f"this conversation was recorded with {self.model_id}; resuming with {model_id} "
                f"may behave differently"
            )
        return warnings

    @property
    def is_resumable(self) -> bool:
        return self.strategy is not ContinuationStrategy.UNSUPPORTED

    def redacted(self) -> dict[str, Any]:
        """A Doctor/UI-safe summary. Never includes native payloads or reasoning text (spec §26)."""

        return {
            "provider_type": self.provider_type,
            "protocol": self.protocol.value,
            "strategy": self.strategy.value,
            "schema_version": self.schema_version,
            "model_id": self.model_id,
            "size_bytes": self.size_bytes,
            "has_remote_id": self.remote_interaction_id is not None,
            "native_step_count": len(self.native_steps),
            "created_at": self.created_at.isoformat(),
        }


def unsupported(provider_type: str, protocol: Protocol, *, reason: str) -> ContinuationEnvelope:
    """An explicit "this cannot be resumed", carrying why.

    Stored rather than left absent, so the UI can explain the absence instead of showing a resume
    affordance that silently starts a new conversation.
    """

    return ContinuationEnvelope.build(
        provider_type=provider_type,
        protocol=protocol,
        strategy=ContinuationStrategy.UNSUPPORTED,
        opaque_fields={"reason": reason},
    )
