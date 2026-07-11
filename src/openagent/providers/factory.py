"""Provider presets + adapter factory (spec §12–§24).

Maps a :class:`ProviderConnection` (protocol + provider_type + base URL) onto a concrete adapter,
and supplies default base URLs / protocols for known providers so the user only needs a key.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.models import Protocol, ProviderConnection
from .anthropic_messages import AnthropicMessagesAdapter
from .base import ProviderAdapter
from .compat.profiles import get_compat
from .openai_chat import OpenAIChatAdapter
from .openai_responses import OpenAIResponsesAdapter


@dataclass(frozen=True)
class ProviderPreset:
    provider_type: str
    label: str
    protocol: Protocol
    openai_base_url: str | None = None
    anthropic_base_url: str | None = None
    needs_key: bool = True
    note: str = ""


#: Built-in presets. Base URLs come straight from each provider's docs (spec §12–§24).
PRESETS: dict[str, ProviderPreset] = {
    "openai": ProviderPreset("openai", "OpenAI", Protocol.OPENAI_RESPONSES,
                             openai_base_url="https://api.openai.com/v1"),
    "anthropic": ProviderPreset("anthropic", "Anthropic", Protocol.ANTHROPIC_MESSAGES,
                                anthropic_base_url="https://api.anthropic.com"),
    "deepseek": ProviderPreset("deepseek", "DeepSeek", Protocol.OPENAI_CHAT,
                               openai_base_url="https://api.deepseek.com",
                               anthropic_base_url="https://api.deepseek.com/anthropic"),
    "qwen": ProviderPreset("qwen", "Alibaba Qwen (Model Studio)", Protocol.OPENAI_CHAT,
                           openai_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
    "kimi": ProviderPreset("kimi", "Kimi / Moonshot", Protocol.OPENAI_CHAT,
                           openai_base_url="https://api.moonshot.cn/v1"),
    "glm": ProviderPreset("glm", "GLM / Z.AI", Protocol.OPENAI_CHAT,
                          openai_base_url="https://api.z.ai/api/paas/v4",
                          anthropic_base_url="https://api.z.ai/api/anthropic"),
    "minimax": ProviderPreset("minimax", "MiniMax", Protocol.ANTHROPIC_MESSAGES,
                              openai_base_url="https://api.minimaxi.com/v1",
                              anthropic_base_url="https://api.minimaxi.com/anthropic"),
    "openrouter": ProviderPreset("openrouter", "OpenRouter", Protocol.OPENAI_CHAT,
                                 openai_base_url="https://openrouter.ai/api/v1"),
    "mistral": ProviderPreset("mistral", "Mistral", Protocol.OPENAI_CHAT,
                              openai_base_url="https://api.mistral.ai/v1"),
    "together": ProviderPreset("together", "Together", Protocol.OPENAI_CHAT,
                               openai_base_url="https://api.together.ai/v1"),
    "fireworks": ProviderPreset("fireworks", "Fireworks", Protocol.OPENAI_CHAT,
                                openai_base_url="https://api.fireworks.ai/inference/v1"),
    "ollama": ProviderPreset("ollama", "Ollama (local)", Protocol.OPENAI_CHAT,
                             openai_base_url="http://localhost:11434/v1", needs_key=False),
    "lmstudio": ProviderPreset("lmstudio", "LM Studio (local)", Protocol.OPENAI_CHAT,
                               openai_base_url="http://localhost:1234/v1", needs_key=False),
    "custom": ProviderPreset("custom", "Custom OpenAI-compatible endpoint", Protocol.OPENAI_CHAT),
}


def get_preset(provider_type: str) -> ProviderPreset | None:
    return PRESETS.get(provider_type)


def preset_names() -> list[str]:
    return list(PRESETS)


def resolve_base_url(provider: ProviderConnection) -> str:
    if provider.protocol is Protocol.ANTHROPIC_MESSAGES:
        url = provider.anthropic_base_url or provider.base_url
    else:
        url = provider.base_url or provider.anthropic_base_url
    if not url:
        preset = get_preset(provider.provider_type)
        if preset:
            url = (
                preset.anthropic_base_url
                if provider.protocol is Protocol.ANTHROPIC_MESSAGES
                else preset.openai_base_url
            )
    if not url:
        raise ValueError(f"no base URL configured for provider {provider.name!r}")
    return url


def build_adapter(provider: ProviderConnection, api_key: str | None) -> ProviderAdapter:
    """Construct the concrete adapter for a provider connection."""

    base_url = resolve_base_url(provider)
    if provider.protocol is Protocol.ANTHROPIC_MESSAGES:
        return AnthropicMessagesAdapter(
            base_url=base_url, api_key=api_key, provider_type=provider.provider_type,
            extra_headers=provider.extra_headers or None,
        )
    if provider.protocol is Protocol.OPENAI_RESPONSES:
        return OpenAIResponsesAdapter(
            base_url=base_url, api_key=api_key, provider_type=provider.provider_type,
            extra_headers=provider.extra_headers or None,
        )
    return OpenAIChatAdapter(
        base_url=base_url, api_key=api_key, provider_type=provider.provider_type,
        extra_headers=provider.extra_headers or None,
        compat=get_compat(provider.provider_type),
    )
