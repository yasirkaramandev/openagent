"""Compatibility profiles v2 (spec §8).

A profile answers one question: *how do I talk to this endpoint?* Field names, parameter bounds,
which knobs to omit, how reasoning is requested and returned, how history has to be shaped on the
way back in.

It deliberately does **not** answer "what can this model do". That distinction is the reason this
file exists. v1 profiles carried `supports_parallel_tools` and friends as tri-state booleans, and
the temptation in every adapter is to read a profile default as a capability fact — at which point
a preset someone typed while reading documentation becomes a claim about a specific deployment of a
specific model revision. Capability truth lives in :mod:`.evidence`, sourced from a probe, a
catalog, a fixture, or an explicit human override.

What a profile *may* say about capabilities is narrower and honest: whether the **protocol shape**
can express the thing at all. `supports_tool_choice_required=False` for Kimi means the API rejects
that parameter — it is a statement about the request format, not about the model's competence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ...core.models import DiscoveryStrategy, Protocol


class AuthScheme(str, Enum):
    BEARER = "bearer"
    API_KEY_HEADER = "api-key-header"
    GOOGLE_API_KEY = "google-api-key"
    NONE = "none"


class ReasoningRequestStyle(str, Enum):
    """How to *ask* for reasoning, which is not standardized across OpenAI-compatible endpoints."""

    NONE = "none"
    #: OpenAI-style `reasoning: {effort: ...}`.
    REASONING_EFFORT = "reasoning-effort"
    #: GLM-style `thinking: {type: "enabled"}`.
    THINKING_OBJECT = "thinking-object"
    #: Anthropic-style `thinking: {type: "enabled", budget_tokens: N}`.
    THINKING_BUDGET = "thinking-budget"
    #: The model reasons unconditionally; nothing is sent.
    IMPLICIT = "implicit"


class AssistantHistoryPolicy(str, Enum):
    """What has to be sent back when continuing a conversation (spec §10).

    This is where "just replay the normalized text" quietly breaks. DeepSeek wants
    `reasoning_content` returned alongside a tool call; MiniMax wants the whole assistant message
    preserved. Dropping those fields does not raise an error — it degrades the next turn, which is
    much harder to notice and much harder to attribute.
    """

    #: Normalized text roles only.
    NORMALIZED_TEXT = "normalized-text"
    #: The provider's own assistant message object, replayed verbatim.
    NATIVE_MESSAGE = "native-message"
    #: Native message *including* reasoning fields, required for tool continuation.
    NATIVE_MESSAGE_WITH_REASONING = "native-message-with-reasoning"


class ToolResultPolicy(str, Enum):
    #: OpenAI-style `role: "tool"` messages keyed by tool_call_id.
    TOOL_ROLE = "tool-role"
    #: Anthropic-style `tool_result` content blocks inside a user message.
    USER_CONTENT_BLOCK = "user-content-block"
    #: Gemini-style function-response parts.
    FUNCTION_RESPONSE_PART = "function-response-part"


@dataclass(frozen=True)
class CompatibilityProfile:
    """How to speak to one provider over one transport (spec §8)."""

    provider_type: str
    transport: Protocol = Protocol.OPENAI_CHAT
    auth_scheme: AuthScheme = AuthScheme.BEARER
    model_discovery: DiscoveryStrategy = DiscoveryStrategy.OPENAI_MODELS

    # --- request shape ---------------------------------------------------------------------
    max_tokens_field: str = "max_tokens"
    temperature_min: float = 0.0
    temperature_max: float = 2.0

    # --- what the *protocol* can express (not what the model can do) -----------------------
    supports_tool_choice_auto: bool = True
    supports_tool_choice_required: bool = True
    #: ``None`` = the request format does not settle it; ask the evidence ledger.
    supports_parallel_tools: bool | None = None

    # --- reasoning -------------------------------------------------------------------------
    reasoning_request_style: ReasoningRequestStyle = ReasoningRequestStyle.NONE
    #: Response field carrying reasoning text, e.g. ``reasoning_content``.
    reasoning_response_field: str | None = None
    #: Whether reasoning must be echoed back when continuing after a tool call (spec §10).
    preserve_reasoning_for_tool_history: bool = False

    # --- streaming -------------------------------------------------------------------------
    stream_usage: bool = True
    #: Whether tool-call arguments arrive as incremental fragments.
    stream_tool_arguments: bool = True
    #: Some endpoints need an explicit opt-in for streamed tool calls (GLM's ``tool_stream``).
    tool_stream_request_field: str | None = None

    # --- history ---------------------------------------------------------------------------
    assistant_history_policy: AssistantHistoryPolicy = AssistantHistoryPolicy.NORMALIZED_TEXT
    tool_result_policy: ToolResultPolicy = ToolResultPolicy.TOOL_ROLE

    # --- server-side state -----------------------------------------------------------------
    supports_server_state: bool = False
    server_state_id_field: str | None = None

    # --- misc ------------------------------------------------------------------------------
    usage_location: str = "usage"
    error_mapper: str = "openai"
    extra_request_fields: dict[str, object] = field(default_factory=dict)
    drop_params: frozenset[str] = field(default_factory=frozenset)

    def clamp_temperature(self, value: float | None) -> float | None:
        if value is None:
            return None
        return max(self.temperature_min, min(self.temperature_max, value))

    def normalize_tool_choice(self, choice: str | None) -> str | None:
        """Map a requested tool_choice onto what the endpoint accepts.

        Degrading ``required`` to ``auto`` is a deliberate, visible downgrade: the caller wanted a
        tool call guaranteed and will get one only if the model chooses to. Adapters should surface
        that rather than pretend the request was honoured.
        """

        if choice is None:
            return None
        if choice == "required" and not self.supports_tool_choice_required:
            return "auto" if self.supports_tool_choice_auto else None
        if choice == "auto" and not self.supports_tool_choice_auto:
            return None
        return choice

    def requires_reasoning_replay(self) -> bool:
        return (
            self.preserve_reasoning_for_tool_history
            or self.assistant_history_policy is AssistantHistoryPolicy.NATIVE_MESSAGE_WITH_REASONING
        )


# ---------------------------------------------------------------- registry
#
# Only deviations that are *documented* are encoded here. Every field left at its default is a
# field nothing has established yet — the adapter sends the common shape and the capability ledger
# decides what the model can do. A profile is allowed to say "this endpoint rejects that
# parameter"; it is not allowed to say "this model supports reasoning", because it has no way to
# know that and a wrong preset is indistinguishable from a verified fact once it is read.

PROFILES_V2: dict[str, CompatibilityProfile] = {
    "openai": CompatibilityProfile(
        "openai",
        transport=Protocol.OPENAI_CHAT,
        max_tokens_field="max_completion_tokens",
        reasoning_request_style=ReasoningRequestStyle.REASONING_EFFORT,
    ),
    "anthropic": CompatibilityProfile(
        "anthropic",
        transport=Protocol.ANTHROPIC_MESSAGES,
        auth_scheme=AuthScheme.API_KEY_HEADER,
        temperature_max=1.0,
        reasoning_request_style=ReasoningRequestStyle.THINKING_BUDGET,
        tool_result_policy=ToolResultPolicy.USER_CONTENT_BLOCK,
        error_mapper="anthropic",
    ),
    # DeepSeek returns reasoning in `reasoning_content`, and wants it back on the next request when
    # the turn included a tool call — dropping it does not error, it just degrades (spec §15).
    "deepseek": CompatibilityProfile(
        "deepseek",
        transport=Protocol.OPENAI_CHAT,
        reasoning_response_field="reasoning_content",
        preserve_reasoning_for_tool_history=True,
        assistant_history_policy=AssistantHistoryPolicy.NATIVE_MESSAGE_WITH_REASONING,
        error_mapper="deepseek",
    ),
    # Kimi rejects tool_choice=required (spec §18). International and China endpoints are separate
    # connections, not one provider with a toggle — a key is valid for exactly one of them.
    "kimi": CompatibilityProfile(
        "kimi",
        transport=Protocol.OPENAI_CHAT,
        supports_tool_choice_required=False,
        temperature_max=1.0,
    ),
    "qwen": CompatibilityProfile("qwen", transport=Protocol.OPENAI_CHAT),
    # GLM: `thinking` object to request reasoning, `reasoning_content` to receive it, and streamed
    # tool calls need an explicit `tool_stream` opt-in (spec §20).
    "glm": CompatibilityProfile(
        "glm",
        transport=Protocol.OPENAI_CHAT,
        reasoning_request_style=ReasoningRequestStyle.THINKING_OBJECT,
        reasoning_response_field="reasoning_content",
        tool_stream_request_field="tool_stream",
        supports_tool_choice_required=False,
        supports_tool_choice_auto=True,
    ),
    # MiniMax wants the whole assistant message replayed, reasoning details included, and rejects
    # temperature 0 and n != 1 (spec §21).
    "minimax": CompatibilityProfile(
        "minimax",
        transport=Protocol.OPENAI_CHAT,
        temperature_min=0.01,
        temperature_max=1.0,
        assistant_history_policy=AssistantHistoryPolicy.NATIVE_MESSAGE_WITH_REASONING,
        preserve_reasoning_for_tool_history=True,
        drop_params=frozenset({"presence_penalty", "frequency_penalty", "n"}),
    ),
    "openrouter": CompatibilityProfile(
        "openrouter",
        transport=Protocol.OPENAI_CHAT,
        model_discovery=DiscoveryStrategy.OPENROUTER_CATALOG,
    ),
    # A local Ollama needs no credential. A *remote* one is a different connection with its own
    # auth, and is not reachable over plain HTTP by default (spec §23).
    "ollama": CompatibilityProfile(
        "ollama",
        transport=Protocol.OLLAMA_NATIVE_CHAT,
        auth_scheme=AuthScheme.NONE,
        model_discovery=DiscoveryStrategy.OLLAMA_TAGS_SHOW,
        error_mapper="ollama",
    ),
    "lmstudio": CompatibilityProfile(
        "lmstudio",
        transport=Protocol.OPENAI_RESPONSES,
        auth_scheme=AuthScheme.NONE,
        model_discovery=DiscoveryStrategy.LMSTUDIO_NATIVE,
        supports_server_state=True,
        server_state_id_field="previous_response_id",
        error_mapper="lmstudio",
    ),
    "gemini": CompatibilityProfile(
        "gemini",
        transport=Protocol.GEMINI_INTERACTIONS,
        auth_scheme=AuthScheme.GOOGLE_API_KEY,
        model_discovery=DiscoveryStrategy.GEMINI_MODELS,
        max_tokens_field="max_output_tokens",
        temperature_max=2.0,
        supports_server_state=True,
        server_state_id_field="previous_interaction_id",
        tool_result_policy=ToolResultPolicy.FUNCTION_RESPONSE_PART,
        error_mapper="gemini",
    ),
    # NVIDIA Build: send only the common fields until a probe or fixture proves more. In
    # particular `stream_options` is not in NVIDIA's documented examples, so streaming usage stays
    # off — an adapter must not send a field the endpoint may reject.
    "nvidia-build": CompatibilityProfile(
        "nvidia-build",
        transport=Protocol.OPENAI_CHAT,
        stream_usage=False,
        supports_tool_choice_required=False,
    ),
}

#: Unknown providers get the permissive OpenAI-compatible shape. It is the most likely to work and
#: the least likely to send something an endpoint rejects.
_DEFAULT_V2 = CompatibilityProfile("generic")


def get_profile(provider_type: str) -> CompatibilityProfile:
    return PROFILES_V2.get(provider_type, _DEFAULT_V2)
