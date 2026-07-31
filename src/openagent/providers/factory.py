"""Provider presets + adapter factory (spec §12–§24).

Maps a :class:`ProviderConnection` (protocol + provider_type + base URL) onto a concrete adapter,
and supplies default base URLs / protocols for known providers so the user only needs a key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

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
    #: Optional richer metadata (spec §10) used by hosted-catalog providers such as NVIDIA Build to
    #: drive a provider-aware credential/model UI without hardcoding anything model-specific.
    default_env_var: str | None = None
    credential_label: str | None = None
    credential_hint: str | None = None
    catalog_url: str | None = None
    docs_url: str | None = None
    model_id_hint: str | None = None
    #: True when ``/models`` mixes model *types* (chat, embedding, rerank, vision…) so a listed model
    #: is NOT automatically an agent-compatible chat model — it must be capability-probed (§14.3).
    catalog_is_mixed: bool = False


#: Built-in presets. Base URLs come straight from each provider's docs (spec §12–§24).
PRESETS: dict[str, ProviderPreset] = {
    "openai": ProviderPreset(
        "openai", "OpenAI", Protocol.OPENAI_RESPONSES, openai_base_url="https://api.openai.com/v1"
    ),
    "anthropic": ProviderPreset(
        "anthropic",
        "Anthropic",
        Protocol.ANTHROPIC_MESSAGES,
        anthropic_base_url="https://api.anthropic.com",
    ),
    "deepseek": ProviderPreset(
        "deepseek",
        "DeepSeek",
        Protocol.OPENAI_CHAT,
        openai_base_url="https://api.deepseek.com",
        anthropic_base_url="https://api.deepseek.com/anthropic",
    ),
    "qwen": ProviderPreset(
        "qwen",
        "Alibaba Qwen (Model Studio)",
        Protocol.OPENAI_CHAT,
        openai_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    ),
    "kimi": ProviderPreset(
        "kimi",
        "Kimi / Moonshot",
        Protocol.OPENAI_CHAT,
        openai_base_url="https://api.moonshot.cn/v1",
    ),
    "glm": ProviderPreset(
        "glm",
        "GLM / Z.AI",
        Protocol.OPENAI_CHAT,
        openai_base_url="https://api.z.ai/api/paas/v4",
        anthropic_base_url="https://api.z.ai/api/anthropic",
    ),
    "minimax": ProviderPreset(
        "minimax",
        "MiniMax",
        Protocol.ANTHROPIC_MESSAGES,
        openai_base_url="https://api.minimaxi.com/v1",
        anthropic_base_url="https://api.minimaxi.com/anthropic",
    ),
    "openrouter": ProviderPreset(
        "openrouter",
        "OpenRouter",
        Protocol.OPENAI_CHAT,
        openai_base_url="https://openrouter.ai/api/v1",
    ),
    # NVIDIA Build — hosted NIM APIs, OpenAI Chat Completions protocol (spec §9, §10). Base URL and
    # protocol come straight from https://docs.api.nvidia.com/nim/reference/llm-apis. This is the
    # HOSTED catalog at build.nvidia.com; self-hosted NIM users configure a `custom` endpoint instead.
    "nvidia-build": ProviderPreset(
        "nvidia-build",
        "NVIDIA Build (Hosted NIM APIs)",
        Protocol.OPENAI_CHAT,
        openai_base_url="https://integrate.api.nvidia.com/v1",
        needs_key=True,
        default_env_var="NVIDIA_API_KEY",
        credential_label="NVIDIA API Key",
        credential_hint="Generate it on build.nvidia.com; keys commonly begin with nvapi-",
        catalog_url="https://build.nvidia.com/",
        docs_url="https://docs.api.nvidia.com/nim/reference/llm-apis",
        model_id_hint="publisher/model",
        catalog_is_mixed=True,
    ),
    "mistral": ProviderPreset(
        "mistral", "Mistral", Protocol.OPENAI_CHAT, openai_base_url="https://api.mistral.ai/v1"
    ),
    "together": ProviderPreset(
        "together", "Together", Protocol.OPENAI_CHAT, openai_base_url="https://api.together.ai/v1"
    ),
    "fireworks": ProviderPreset(
        "fireworks",
        "Fireworks",
        Protocol.OPENAI_CHAT,
        openai_base_url="https://api.fireworks.ai/inference/v1",
    ),
    "ollama": ProviderPreset(
        "ollama",
        "Ollama (local)",
        Protocol.OPENAI_CHAT,
        openai_base_url="http://localhost:11434/v1",
        needs_key=False,
    ),
    "lmstudio": ProviderPreset(
        "lmstudio",
        "LM Studio (local)",
        Protocol.OPENAI_CHAT,
        openai_base_url="http://localhost:1234/v1",
        needs_key=False,
    ),
    # Gemini speaks its own protocol, so it has no OpenAI-compatible base URL. Without a preset it
    # is implemented and unreachable: the wizard builds its provider list from this table.
    "gemini": ProviderPreset(
        "gemini",
        "Google Gemini (Interactions API)",
        Protocol.GEMINI_INTERACTIONS,
        needs_key=True,
        default_env_var="GEMINI_API_KEY",
        credential_label="Gemini API key",
        credential_hint="From Google AI Studio. Vertex AI (ADC) is a separate provider, not this one.",
        docs_url="https://ai.google.dev/api",
    ),
    "custom": ProviderPreset("custom", "Custom OpenAI-compatible endpoint", Protocol.OPENAI_CHAT),
}


def get_preset(provider_type: str) -> ProviderPreset | None:
    return PRESETS.get(provider_type)


def preset_names() -> list[str]:
    return list(PRESETS)


def is_nvidia_build_endpoint(value: str | None) -> bool:
    """Whether a URL is exactly NVIDIA Build's hosted endpoint after safe normalization.

    Scheme/host casing, the default HTTPS port and a trailing slash are equivalent. User info,
    query strings, fragments, non-default ports and any other path may denote a gateway or custom
    endpoint, so those are intentionally not treated as NVIDIA Build.
    """

    if not value:
        return False
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower() == "integrate.api.nvidia.com"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path.rstrip("/") == "/v1"
        and not parsed.query
        and not parsed.fragment
    )


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
    """Construct the concrete adapter for a provider connection.

    The single chokepoint between a stored connection and something that can talk to it, which is
    why the v0.2 routing decision lives here rather than in each caller: provider_service and
    preflight both go through this, so a provider is either upgraded for both or for neither.

    A provider with a :mod:`~.spec` entry is served by :class:`~.wire_adapter.WireProviderAdapter` —
    the shared protocol wires, the evidence-aware probe, the region-bound endpoint resolution.
    Everything else (openai, anthropic, nvidia-build, custom, and the generic OpenAI-compatible
    presets) keeps the v0.1 adapters, which are still the right implementation for them.
    """

    from .spec import get_spec

    spec = get_spec(provider.provider_type)
    if spec is not None:
        return _build_v2_adapter(provider, api_key, spec)

    base_url = resolve_base_url(provider)
    if provider.protocol is Protocol.ANTHROPIC_MESSAGES:
        return AnthropicMessagesAdapter(
            base_url=base_url,
            api_key=api_key,
            provider_type=provider.provider_type,
            extra_headers=provider.extra_headers or None,
        )
    if provider.protocol is Protocol.OPENAI_RESPONSES:
        return OpenAIResponsesAdapter(
            base_url=base_url,
            api_key=api_key,
            provider_type=provider.provider_type,
            extra_headers=provider.extra_headers or None,
        )
    return OpenAIChatAdapter(
        base_url=base_url,
        api_key=api_key,
        provider_type=provider.provider_type,
        extra_headers=provider.extra_headers or None,
        compat=get_compat(provider.provider_type),
    )


def _build_v2_adapter(
    provider: ProviderConnection, api_key: str | None, spec: Any
) -> ProviderAdapter:
    """Build a v0.2 adapter from a stored connection.

    The connection's own protocol and region win over the spec's defaults — that is what makes the
    row meaningful — but a row whose protocol the provider does not serve in that region must not
    silently be served over a different one. ``spec.resolve`` raises for exactly that case, and the
    error names both sides.

    ``base_url`` is passed through when the row carries one, so a self-hosted endpoint or a gateway
    keeps working; the region still says which credential population the key belongs to.
    """

    from .wire_adapter import WireProviderAdapter

    protocol = provider.protocol if provider.protocol in spec.protocols else None
    base_url = provider.base_url or provider.anthropic_base_url or None

    return WireProviderAdapter(
        spec=spec,
        api_key=api_key,
        region=provider.region or None,
        protocol=protocol,
        base_url=base_url,
        workspace_id=provider.workspace_id or None,
        extra_headers=provider.extra_headers or None,
        # A local provider on a loopback address is exempt from the TLS requirement; anything else
        # would need an explicit opt-in, which a stored row cannot grant on the user's behalf.
        allow_insecure_http=False,
    )
