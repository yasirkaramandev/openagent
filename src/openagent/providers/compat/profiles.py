"""Compatibility profiles (spec §26).

"OpenAI-compatible" never means *fully* compatible. A :class:`CompatibilityProfile` captures the
per-provider deviations the transport/adapters must normalize: tool_choice support, temperature
bounds, whether streaming usage is emitted, the max-token field name, and so on. Capability *truth*
still comes from probing a specific model (spec §20), not from these presets.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CompatibilityProfile:
    provider_type: str
    #: OpenAI ``max_tokens`` vs ``max_completion_tokens`` etc.
    max_tokens_field: str = "max_tokens"
    supports_tool_choice_required: bool = True
    supports_tool_choice_auto: bool = True
    supports_parallel_tools: bool | None = None  # None → determine by probe
    stream_usage: bool = True
    temperature_min: float = 0.0
    temperature_max: float = 2.0
    #: Extra request params to strip because the provider rejects them.
    drop_params: frozenset[str] = field(default_factory=frozenset)

    def clamp_temperature(self, value: float | None) -> float | None:
        if value is None:
            return None
        return max(self.temperature_min, min(self.temperature_max, value))

    def normalize_tool_choice(self, choice: str | None) -> str | None:
        """Map a requested tool_choice onto what the provider accepts (spec §17 Kimi)."""

        if choice is None:
            return None
        if choice == "required" and not self.supports_tool_choice_required:
            return "auto" if self.supports_tool_choice_auto else None
        if choice == "auto" and not self.supports_tool_choice_auto:
            return None
        return choice


#: Known presets. Unknown providers fall back to a permissive OpenAI-compatible default.
PROFILES: dict[str, CompatibilityProfile] = {
    "openai": CompatibilityProfile("openai", max_tokens_field="max_completion_tokens"),
    "anthropic": CompatibilityProfile("anthropic", temperature_max=1.0),
    "deepseek": CompatibilityProfile("deepseek"),
    "kimi": CompatibilityProfile(  # tool_choice=required unsupported (spec §17)
        "kimi", supports_tool_choice_required=False, temperature_max=1.0,
    ),
    "qwen": CompatibilityProfile("qwen"),
    "glm": CompatibilityProfile("glm"),
    "minimax": CompatibilityProfile("minimax"),
    "openrouter": CompatibilityProfile("openrouter"),
    # NVIDIA Build (spec §11). Send ONLY the common OpenAI-compatible fields until a capability probe
    # (or an official fixture) proves more. In particular ``stream_options`` is NOT documented in
    # NVIDIA's official examples, so streaming usage is off by default — the adapter must not send a
    # field the endpoint may reject. tool_choice=required and parallel tools stay unassumed (probe).
    "nvidia-build": CompatibilityProfile(
        "nvidia-build", stream_usage=False, supports_tool_choice_required=False,
    ),
    "ollama": CompatibilityProfile("ollama"),
    "mistral": CompatibilityProfile("mistral"),
    "together": CompatibilityProfile("together"),
    "fireworks": CompatibilityProfile("fireworks"),
}

_DEFAULT = CompatibilityProfile("generic")


def get_compat(provider_type: str) -> CompatibilityProfile:
    return PROFILES.get(provider_type, _DEFAULT)
