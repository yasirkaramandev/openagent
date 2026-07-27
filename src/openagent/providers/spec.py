"""What makes one v0.2 provider different from another, as data (spec §12–§21).

Nine providers, and almost all of their differences are values: an endpoint per region, which
protocols they speak and in what preference order, how their catalog is listed, which environment
variables carry their credential, whether they are local. Encoded as data, adding a tenth provider is
a table entry; encoded as code, it is a tenth adapter that reimplements streaming.

Two of these values are load-bearing in ways that are easy to miss.

**Regions are separate deployments, not a setting.** A Kimi key is valid on exactly one of the
international and China endpoints, and a Qwen key belongs to one region and workspace. Pointing a
valid key at the wrong endpoint returns 401 — indistinguishable from an invalid key unless the
endpoint's identity is known locally, which is why the region is part of the connection rather than a
toggle on the request (spec §16.2, §18.1).

**Protocol preference is ordered and the order has reasons.** LM Studio is offered Responses first
because only Responses can hold state server-side; MiniMax is offered Anthropic Messages first
because its thinking blocks and signatures survive there and are lossy over its chat endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..core.models import CredentialType, DiscoveryStrategy, Protocol
from .compat.profiles_v2 import AuthScheme, get_profile

#: Hosts a plain-HTTP connection is allowed to. Anything else must be TLS: a bearer token over
#: cleartext to a non-loopback host is a credential on the wire (spec §23.3).
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})


@dataclass(frozen=True)
class Region:
    """One deployment of a provider. A credential belongs to exactly one of these."""

    id: str
    label: str
    #: Base URL per protocol. A region that does not serve a protocol simply omits it, which is how
    #: "Responses is not available in this region" gets represented without a second table.
    endpoints: dict[Protocol, str] = field(default_factory=dict)
    #: Free-text note shown in the wizard, e.g. that a workspace id is required here.
    note: str = ""

    def url_for(self, protocol: Protocol) -> str | None:
        return self.endpoints.get(protocol)


@dataclass(frozen=True)
class ProviderSpec:
    """Everything the shared adapter needs to talk to one provider."""

    provider_type: str
    label: str
    #: Preference order. The first entry a region serves is the default.
    protocols: tuple[Protocol, ...]
    discovery: DiscoveryStrategy
    regions: tuple[Region, ...] = ()
    default_region: str | None = None
    #: Credential environment variables this provider documents, in precedence order. Names only.
    env_vars: tuple[str, ...] = ()
    credential_label: str = "API key"
    credential_hint: str = ""
    needs_key: bool = True
    #: A service the user runs. Local providers get the loopback HTTP exemption and a manager.
    local: bool = False
    #: Whether a workspace/project id is part of the connection (Qwen).
    needs_workspace: bool = False
    #: Provider-side conversation retention is possible here. Off by default wherever it exists —
    #: turning it on is a privacy decision the user makes knowingly (spec §10.4, §13.3, §16.5).
    supports_server_state: bool = False
    #: A provider token-counting endpoint, when one exists. Absent means the local estimate is used
    #: and is reported *as* an estimate rather than as a provider-authoritative count.
    token_count_path: str | None = None
    experimental: bool = False
    docs_url: str = ""
    notes: str = ""

    # ------------------------------------------------------------------ resolution

    def region(self, region_id: str | None) -> Region | None:
        wanted = region_id or self.default_region
        if wanted is None:
            return self.regions[0] if self.regions else None
        for region in self.regions:
            if region.id == wanted:
                return region
        return None

    def resolve(
        self, *, region_id: str | None = None, protocol: Protocol | None = None
    ) -> tuple[Protocol, str]:
        """Pick the protocol and base URL for a connection.

        Raises rather than falling back to a different region's endpoint. A silent fallback is how a
        China-region key ends up authenticating against the international endpoint and reporting an
        invalid key.
        """

        region = self.region(region_id)
        if region is None:
            raise ValueError(
                f"{self.label} has no region {region_id!r}; known: "
                f"{[r.id for r in self.regions] or 'none'}"
            )
        if protocol is not None:
            url = region.url_for(protocol)
            if url is None:
                raise ValueError(
                    f"{self.label} region {region.id!r} does not serve {protocol.value}; "
                    f"it serves {[p.value for p in region.endpoints]}"
                )
            return protocol, url
        for candidate in self.protocols:
            url = region.url_for(candidate)
            if url is not None:
                return candidate, url
        raise ValueError(f"{self.label} region {region.id!r} serves none of its declared protocols")

    def supported_protocols(self, region_id: str | None = None) -> tuple[Protocol, ...]:
        region = self.region(region_id)
        if region is None:
            return ()
        return tuple(p for p in self.protocols if p in region.endpoints)

    def credential_type(self) -> CredentialType:
        return CredentialType.NONE if not self.needs_key else CredentialType.KEYCHAIN

    def auth_scheme(self) -> AuthScheme:
        return get_profile(self.provider_type).auth_scheme


def requires_tls(url: str, *, local: bool) -> bool:
    """Whether ``url`` must be HTTPS.

    A local provider on loopback is exempt: there is no network to intercept, and forcing TLS on
    ``http://localhost:11434`` would make Ollama unusable out of the box. A local provider reached
    over a *network* address is not exempt — that is a remote connection wearing a local provider's
    name, and it is exactly the case where a token would cross the wire in cleartext (spec §23.3).
    """

    try:
        parsed = urlsplit(url)
    except ValueError:
        return True
    if parsed.scheme == "https":
        return False
    host = (parsed.hostname or "").lower()
    if local and host in LOOPBACK_HOSTS:
        return False
    return True


def is_loopback(url: str) -> bool:
    try:
        return (urlsplit(url).hostname or "").lower() in LOOPBACK_HOSTS
    except ValueError:
        return False


# --------------------------------------------------------------------------- the nine


_OPENAI_CHAT = Protocol.OPENAI_CHAT
_RESPONSES = Protocol.OPENAI_RESPONSES
_ANTHROPIC = Protocol.ANTHROPIC_MESSAGES

SPECS: dict[str, ProviderSpec] = {
    # --- Gemini (§10) --------------------------------------------------------------------
    "gemini": ProviderSpec(
        provider_type="gemini",
        label="Google Gemini (Interactions API)",
        protocols=(Protocol.GEMINI_INTERACTIONS,),
        discovery=DiscoveryStrategy.GEMINI_MODELS,
        regions=(
            Region(
                "global",
                "Global",
                {Protocol.GEMINI_INTERACTIONS: "https://generativelanguage.googleapis.com/v1beta"},
            ),
        ),
        default_region="global",
        # Vertex AI is deliberately *not* here. It authenticates with Application Default
        # Credentials against a project/location, which is a different credential type and a
        # different endpoint family — folding it in behind the same "API key" would make one
        # provider row mean two incompatible things (spec §10.1).
        env_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        credential_label="Gemini API key",
        credential_hint="From Google AI Studio. Vertex AI (ADC) is a separate provider, not this one.",
        supports_server_state=True,
        docs_url="https://ai.google.dev/api",
    ),
    # --- Ollama (§12) -------------------------------------------------------------------
    "ollama": ProviderSpec(
        provider_type="ollama",
        label="Ollama (local)",
        protocols=(Protocol.OLLAMA_NATIVE_CHAT, _OPENAI_CHAT),
        discovery=DiscoveryStrategy.OLLAMA_TAGS_SHOW,
        regions=(
            Region(
                "local",
                "This machine",
                {
                    Protocol.OLLAMA_NATIVE_CHAT: "http://localhost:11434",
                    _OPENAI_CHAT: "http://localhost:11434/v1",
                },
                note="No credential needed on loopback.",
            ),
            Region(
                "remote",
                "Remote Ollama host",
                {
                    Protocol.OLLAMA_NATIVE_CHAT: "https://ollama.example",
                    _OPENAI_CHAT: "https://ollama.example/v1",
                },
                note="TLS required; the URL must be replaced with your host.",
            ),
        ),
        default_region="local",
        needs_key=False,
        local=True,
        docs_url="https://github.com/ollama/ollama/blob/main/docs/api.md",
        notes="Native chat is preferred: it keeps `thinking` separate from content and preserves the "
        "assistant message a tool continuation replays.",
    ),
    # --- LM Studio (§13) ----------------------------------------------------------------
    "lmstudio": ProviderSpec(
        provider_type="lmstudio",
        label="LM Studio (local)",
        # Responses first: it is the only one of the four that can hold state server-side.
        protocols=(_RESPONSES, _OPENAI_CHAT, _ANTHROPIC, Protocol.LMSTUDIO_NATIVE_CHAT),
        discovery=DiscoveryStrategy.LMSTUDIO_NATIVE,
        regions=(
            Region(
                "local",
                "This machine",
                {
                    _RESPONSES: "http://localhost:1234/v1",
                    _OPENAI_CHAT: "http://localhost:1234/v1",
                    _ANTHROPIC: "http://localhost:1234",
                    Protocol.LMSTUDIO_NATIVE_CHAT: "http://localhost:1234",
                },
                note="No credential needed on loopback.",
            ),
            Region(
                "remote",
                "Remote LM Studio host",
                {
                    _RESPONSES: "https://lmstudio.example/v1",
                    _OPENAI_CHAT: "https://lmstudio.example/v1",
                },
                note="TLS required; the URL must be replaced with your host.",
            ),
        ),
        default_region="local",
        needs_key=False,
        local=True,
        supports_server_state=True,
        docs_url="https://lmstudio.ai/docs/app/api",
    ),
    # --- OpenRouter (§14) ---------------------------------------------------------------
    "openrouter": ProviderSpec(
        provider_type="openrouter",
        label="OpenRouter",
        protocols=(_OPENAI_CHAT,),
        discovery=DiscoveryStrategy.OPENROUTER_CATALOG,
        regions=(Region("global", "Global", {_OPENAI_CHAT: "https://openrouter.ai/api/v1"}),),
        default_region="global",
        env_vars=("OPENROUTER_API_KEY",),
        credential_label="OpenRouter API key",
        docs_url="https://openrouter.ai/docs",
        notes="Routing policy is a separate object, never embedded in the credential (spec §14.3).",
    ),
    # --- DeepSeek (§15) -----------------------------------------------------------------
    "deepseek": ProviderSpec(
        provider_type="deepseek",
        label="DeepSeek",
        protocols=(_OPENAI_CHAT, _ANTHROPIC),
        discovery=DiscoveryStrategy.OPENAI_MODELS,
        regions=(
            Region(
                "global",
                "Global",
                {
                    _OPENAI_CHAT: "https://api.deepseek.com",
                    _ANTHROPIC: "https://api.deepseek.com/anthropic",
                },
            ),
        ),
        default_region="global",
        env_vars=("DEEPSEEK_API_KEY",),
        credential_label="DeepSeek API key",
        docs_url="https://api-docs.deepseek.com",
        notes="reasoning_content must be replayed with the tool call it accompanied (spec §15.3).",
    ),
    # --- Qwen (§16, §17) ----------------------------------------------------------------
    "qwen": ProviderSpec(
        provider_type="qwen",
        label="Alibaba Qwen (Model Studio)",
        protocols=(_OPENAI_CHAT, _RESPONSES, _ANTHROPIC),
        # Model Studio's compatible-mode `/models` is not a reliable catalog across regions, so the
        # versioned manifest is the starting point and a manual id is always available (spec §16.4).
        discovery=DiscoveryStrategy.CURATED_CATALOG,
        regions=(
            Region(
                "intl",
                "International (Singapore)",
                {
                    _OPENAI_CHAT: "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                    _RESPONSES: "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                    _ANTHROPIC: "https://dashscope-intl.aliyuncs.com/apps/anthropic",
                },
                note="Keys issued in the international console work only here.",
            ),
            Region(
                "cn",
                "China (Beijing)",
                {
                    _OPENAI_CHAT: "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    _RESPONSES: "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    _ANTHROPIC: "https://dashscope.aliyuncs.com/apps/anthropic",
                },
                note="Keys issued in the China console work only here.",
            ),
        ),
        default_region="intl",
        env_vars=("DASHSCOPE_API_KEY", "QWEN_API_KEY"),
        credential_label="DashScope API key",
        credential_hint="Region-bound: an international key is rejected by the China endpoint.",
        needs_workspace=True,
        supports_server_state=True,
        docs_url="https://www.alibabacloud.com/help/en/model-studio",
    ),
    # --- Kimi / Moonshot (§18) ----------------------------------------------------------
    "kimi": ProviderSpec(
        provider_type="kimi",
        label="Kimi / Moonshot",
        protocols=(_OPENAI_CHAT, _ANTHROPIC),
        discovery=DiscoveryStrategy.OPENAI_MODELS,
        regions=(
            Region(
                "intl",
                "International",
                {
                    _OPENAI_CHAT: "https://api.moonshot.ai/v1",
                    _ANTHROPIC: "https://api.moonshot.ai/anthropic",
                },
                note="Keys from platform.moonshot.ai.",
            ),
            Region(
                "cn",
                "China",
                {
                    _OPENAI_CHAT: "https://api.moonshot.cn/v1",
                    _ANTHROPIC: "https://api.moonshot.cn/anthropic",
                },
                note="Keys from platform.moonshot.cn.",
            ),
        ),
        default_region="intl",
        env_vars=("MOONSHOT_API_KEY", "KIMI_API_KEY"),
        credential_label="Moonshot API key",
        credential_hint="Region-bound: a .cn key is rejected by the .ai endpoint and vice versa.",
        token_count_path="/tokenizers/estimate-token-count",
        docs_url="https://platform.moonshot.ai/docs",
    ),
    # --- GLM / Z.AI (§19) ---------------------------------------------------------------
    "glm": ProviderSpec(
        provider_type="glm",
        label="GLM (Z.AI)",
        protocols=(_OPENAI_CHAT, _ANTHROPIC),
        discovery=DiscoveryStrategy.OPENAI_MODELS,
        regions=(
            Region(
                "global",
                "Z.AI (global)",
                {
                    _OPENAI_CHAT: "https://api.z.ai/api/paas/v4",
                    _ANTHROPIC: "https://api.z.ai/api/anthropic",
                },
            ),
            Region(
                "cn",
                "Zhipu (China)",
                {
                    _OPENAI_CHAT: "https://open.bigmodel.cn/api/paas/v4",
                    _ANTHROPIC: "https://open.bigmodel.cn/api/anthropic",
                },
            ),
        ),
        default_region="global",
        env_vars=("ZHIPUAI_API_KEY", "GLM_API_KEY", "ZAI_API_KEY"),
        credential_label="Z.AI / Zhipu API key",
        docs_url="https://docs.z.ai",
        notes="This is the native GLM provider. NVIDIA-hosted and Alibaba-hosted GLM are different "
        "connections with different credentials and must not be merged into this row (spec §19).",
    ),
    # --- MiniMax (§21) ------------------------------------------------------------------
    "minimax": ProviderSpec(
        provider_type="minimax",
        label="MiniMax",
        # Anthropic Messages first: thinking blocks and their signatures survive there and are lossy
        # over the chat endpoint (spec §21.3).
        protocols=(_ANTHROPIC, _OPENAI_CHAT),
        discovery=DiscoveryStrategy.OPENAI_MODELS,
        regions=(
            Region(
                "intl",
                "International",
                {
                    _ANTHROPIC: "https://api.minimaxi.com/anthropic",
                    _OPENAI_CHAT: "https://api.minimaxi.com/v1",
                },
            ),
            Region(
                "cn",
                "China",
                {
                    _ANTHROPIC: "https://api.minimax.chat/anthropic",
                    _OPENAI_CHAT: "https://api.minimax.chat/v1",
                },
            ),
        ),
        default_region="intl",
        env_vars=("MINIMAX_API_KEY",),
        credential_label="MiniMax API key",
        docs_url="https://platform.minimaxi.com/document",
    ),
}


def get_spec(provider_type: str) -> ProviderSpec | None:
    return SPECS.get(provider_type)


def spec_names() -> list[str]:
    return sorted(SPECS)


def local_specs() -> list[ProviderSpec]:
    return [spec for spec in SPECS.values() if spec.local]
