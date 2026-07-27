# API providers (v0.2)

Nine providers. Each row below is what OpenAgent actually implements and what has actually been
verified — those are different things, and the "Live verification" column is the difference.

A note on how to read this file. **"Supported" here means the wire format is implemented and the
offline contract suite passes.** It does not mean a given model can do a given thing; that is
capability *evidence*, it is per model, and it is shown in the wizard with its source. Nothing in
this document should be read as a claim about a model.

## How a provider is put together

A provider is not a class. It is a row in [`providers/spec.py`](../src/openagent/providers/spec.py) —
regions, protocol preference, discovery strategy, credential variables — plus a
[`CompatibilityProfile`](../src/openagent/providers/compat/profiles_v2.py) describing how its endpoint
deviates, served by one of five shared protocol wires in
[`providers/wire/`](../src/openagent/providers/wire/).

```
ProviderSpec        who am I talking to, where, and with which credential
CompatibilityProfile how does this endpoint deviate from the protocol
wire/*.py            how is this protocol serialized and read
model_catalog.py     how is this provider's catalog listed
CapabilityLedger     what can this specific model actually do, and who says so
```

Adding a tenth provider that speaks an existing protocol is a table entry.

## Protocol support

| Provider | Protocols (preference order) | Discovery | Regions |
|---|---|---|---|
| Gemini | `gemini-interactions` | `gemini-models` | global |
| Ollama | `ollama-native-chat`, `openai-chat` | `ollama-tags-show` | local, remote |
| LM Studio | `openai-responses`, `openai-chat`, `anthropic-messages`, `lmstudio-native-chat` | `lmstudio-native` | local, remote |
| OpenRouter | `openai-chat` | `openrouter-catalog` | global |
| DeepSeek | `openai-chat`, `anthropic-messages` | `openai-models` | global |
| Qwen | `openai-chat`, `openai-responses`, `anthropic-messages` | `curated-catalog` | intl, cn |
| Kimi | `openai-chat`, `anthropic-messages` | `openai-models` | intl, cn |
| GLM | `openai-chat`, `anthropic-messages` | `openai-models` | global, cn |
| MiniMax | `anthropic-messages`, `openai-chat` | `openai-models` | intl, cn |

Preference order has reasons. LM Studio is offered Responses first because only Responses can hold
state server-side. MiniMax is offered Anthropic Messages first because its thinking blocks carry
signatures that survive there and are lossy over its chat endpoint.

## Regions are separate deployments

For Qwen, Kimi, GLM and MiniMax a credential is valid on **exactly one** region's endpoint. Pointing a
valid key at the wrong one returns 401, which is indistinguishable from an invalid key unless the
endpoint's identity is known locally. So the region is part of the connection, an unknown region
raises rather than falling back, and a 401 whose body mentions the other region is classified as
`provider_region_mismatch` rather than `authentication_failed`.

## Server-side state

Gemini, Qwen (Responses) and LM Studio (Responses) can have the provider hold the conversation.

**Off by default, everywhere.** Turning it on means the provider retains the conversation and resuming
depends on them still having it, so the wizard discloses that at the step where it is enabled, in
those words. `previous_response_id` / `previous_interaction_id` sent while `store` is off is refused
locally — the provider's own error ("unknown response") names the wrong cause.

---

## Per-provider

### Gemini (Interactions API)

- **Auth** — `GEMINI_API_KEY` or `GOOGLE_API_KEY`, sent as the `x-goog-api-key` header. Never the
  `?key=` query parameter: a key in a URL lands in proxy logs and anything recording a request line.
- **Vertex AI is not this provider.** It authenticates with Application Default Credentials against a
  project and location — a different credential type and endpoint family. Folding it in would make one
  provider row mean two incompatible things.
- **Endpoint** — `https://generativelanguage.googleapis.com/v1beta`
- **Model discovery** — paged `/models`, preserving input/output token limits, supported generation
  methods, version and description.
- **Tools** — function calls as steps; arguments accumulated across `arguments_delta` fragments and
  parsed once at `step.stop`.
- **Reasoning** — thought steps carry `thought_signature` values that a *stateless* continuation must
  replay verbatim, so native steps are captured alongside the normalized events.
- **Resume** — `REMOTE_ID` when `store=True`; otherwise native step replay.
- **Known limitation** — the SSE framing of the streamed response is inferred from the endpoint
  family's convention and the `done` sentinel, not from an observed live stream. Status:
  `SSE_FRAMING_LIVE_UNVERIFIED`.
- **Live verification** — `BLOCKED_BY_CREDENTIAL`.

### Ollama (local)

- **Auth** — none on loopback. A *remote* Ollama may carry a bearer token; `AuthScheme.NONE` means
  "not required", not "not accepted".
- **Endpoint** — `http://localhost:11434` (local), TLS required for any non-loopback host.
- **Model discovery** — `/api/tags`, then a bounded concurrent `/api/show` fan-out for capabilities and
  context length, plus `/api/ps` for what is resident. Capabilities come from `show` metadata, never
  from the model's name.
- **Transport** — native `/api/chat`, which is **NDJSON, not SSE**. An SSE reader drops every line and
  reports a successful empty turn.
- **Tools** — Ollama sends no tool-call ids and matches results by `tool_name`, so OpenAgent
  synthesizes a reversible id (`ollama:<index>:<name>`) that round-trips back to the name.
- **Reasoning** — `thinking` is a first-class field, kept out of `content` and preserved for replay.
- **Resume** — native message replay when the turn reasoned or called a tool; normalized otherwise.
- **Known limitation** — Ollama documents no `tool_choice`, so a request for a guaranteed tool call is
  reported as a visible downgrade.
- **Live verification** — `LIVE_VERIFIED` in CI (the workflow starts a real daemon and pulls
  `qwen3:0.6b`); `PROVIDER_UNAVAILABLE` on a machine without one.

### LM Studio (local)

- **Auth** — none on loopback; bearer accepted for a remote host, TLS required there.
- **Model discovery** — `/api/v0/models`, falling back to `/api/v1/models` then `/v1/models`. The
  native path has moved between builds, so it is probed and the answer records which path replied.
- **Model identity** — architecture + quantization. Two local files a user would call by the same name
  behave differently enough that capability evidence must be bound to the pair.
- **Resume** — `previous_response_id` over Responses, when server state is explicitly enabled.
- **Known limitation** — embeddings models are listed and marked non-chat; that is the one negative a
  listing is allowed to assert, because the provider stated the type outright.
- **Live verification** — `PROVIDER_UNAVAILABLE` (no LM Studio in CI).

### OpenRouter

- **Auth** — `OPENROUTER_API_KEY`, bearer.
- **Model discovery** — the full catalog, including `supported_parameters`, modalities, context length
  and pricing. This is the **one** catalog allowed to record `UNSUPPORTED`: its parameter list is
  documented as exhaustive, so absence is a real negative rather than silence.
- **Routing** — [`OpenRouterRoutePolicy`](../src/openagent/providers/openrouter.py) is a separate
  object, never embedded in the credential: rotating a key should not rewrite routing, and reading
  routing should not require reading a secret. Nothing is inferred — a heuristic that silently picks
  the cheapest upstream also silently picks its data-retention terms.
- **Known limitation** — a model's listed capabilities are the *router's* claims about upstreams that
  may change; a live probe outranks them.
- **Live verification** — `BLOCKED_BY_CREDENTIAL`.

### DeepSeek

- **Auth** — `DEEPSEEK_API_KEY`, bearer. **Endpoint** — `https://api.deepseek.com`.
- **Reasoning** — returned in `reasoning_content`, and it must be **replayed with the tool call it
  accompanied**. Dropping it does not error; it degrades the next turn, which is the failure nobody
  attributes later. The profile marks this and the wire honours it.
- **Errors** — 402 is `insufficient_balance`, which nothing else in the default mapping produces; a
  spent account would otherwise read as a permissions problem. 422 is a validation error.
- **Live verification** — `BLOCKED_BY_CREDENTIAL`.

### Qwen (Alibaba Model Studio)

- **Auth** — `DASHSCOPE_API_KEY` or `QWEN_API_KEY`. Region-bound. A workspace id is scoped by the
  `X-DashScope-WorkSpace` header.
- **Model discovery** — a **versioned curated manifest**, because compatible-mode `/models` is not a
  reliable catalog across regions. The manifest is the weakest evidence source and is labelled
  `CURATED_PRESET`; a manual model id is always available. Runtime documentation scraping is
  explicitly not done — a parser aimed at someone's docs page breaks silently and invents models.
- **Protocols** — Chat, Responses and Anthropic Messages are all implemented and endpoint-mapped per
  region.
- **Known limitation** — the Responses and Anthropic paths are implemented and offline-tested but have
  not been exercised against a live region.
- **Live verification** — `BLOCKED_BY_CREDENTIAL`.

### Kimi (Moonshot)

- **Auth** — `MOONSHOT_API_KEY` or `KIMI_API_KEY`. A `.cn` key is rejected by the `.ai` endpoint and
  vice versa.
- **Token counting** — Kimi's own estimate endpoint is used when available; otherwise the local
  estimate is returned and reported *as* an estimate rather than as a provider-authoritative count.
- **Known limitation** — Kimi rejects `tool_choice=required`; the profile avoids sending it and a
  request for a guaranteed call is reported as a visible downgrade.
- **Live verification** — `BLOCKED_BY_CREDENTIAL`.

### GLM (Z.AI / Zhipu)

- **Auth** — `ZHIPUAI_API_KEY`, `GLM_API_KEY` or `ZAI_API_KEY`.
- **This is the native GLM provider.** NVIDIA-hosted GLM and Alibaba-hosted GLM are different
  connections with different credentials and are not merged into this row.
- **Reasoning** — requested with a `thinking` object, returned in `reasoning_content`.
- **Streamed tools** — GLM needs an explicit `tool_stream: true` opt-in before it will stream tool-call
  arguments. It is sent only when streaming *and* tools are present; on a non-stream request the field
  describes a mode that is not in use.
- **Live verification** — `BLOCKED_BY_CREDENTIAL`.

### MiniMax

- **Auth** — `MINIMAX_API_KEY`.
- **Protocol** — Anthropic Messages first. Its assistant turn is an ordered list of typed blocks, and a
  thinking block replayed without its `signature`, or reordered, is rejected outright — so the block
  list is preserved whole rather than flattened and reassembled.
- **Errors** — MiniMax reports application-level failures with **HTTP 200** and a code in
  `base_resp.status_code`. A caller reading only the HTTP status treats those as successful empty
  completions; the error mapper reads the body.
- **Live verification** — `BLOCKED_BY_CREDENTIAL`.

---

## Capability matrix

What the **implementation** supports per provider. Again: this is about the wire, not about any model.

| Provider | Text | Stream | Tools | Parallel tools | Reasoning capture | Server resume | Client native resume | Token counting |
|---|---|---|---|---|---|---|---|---|
| Gemini | ✅ | ✅ | ✅ | ✅ | ✅ (steps) | ✅ | ✅ | estimate |
| Ollama | ✅ | ✅ | ✅ | ✅ | ✅ (`thinking`) | — | ✅ | estimate |
| LM Studio | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | estimate |
| OpenRouter | ✅ | ✅ | ✅ | ✅ | ✅ | — | ✅ | estimate |
| DeepSeek | ✅ | ✅ | ✅ | ✅ | ✅ (`reasoning_content`) | — | ✅ | estimate |
| Qwen | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | estimate |
| Kimi | ✅ | ✅ | ✅ | ✅ | ✅ | — | ✅ | endpoint |
| GLM | ✅ | ✅ | ✅ | ✅ | ✅ (`reasoning_content`) | — | ✅ | estimate |
| MiniMax | ✅ | ✅ | ✅ | ✅ | ✅ (thinking blocks) | — | ✅ | estimate |

## Live verification status

| Provider | Status | Why |
|---|---|---|
| Gemini | `BLOCKED_BY_CREDENTIAL` | no key configured; SSE framing additionally `SSE_FRAMING_LIVE_UNVERIFIED` |
| Ollama | `LIVE_VERIFIED` (CI) | the workflow starts a real daemon |
| LM Studio | `PROVIDER_UNAVAILABLE` | no LM Studio in CI |
| OpenRouter | `BLOCKED_BY_CREDENTIAL` | no key configured |
| DeepSeek | `BLOCKED_BY_CREDENTIAL` | no key configured |
| Qwen | `BLOCKED_BY_CREDENTIAL` | no key configured |
| Kimi | `BLOCKED_BY_CREDENTIAL` | no key configured |
| GLM | `BLOCKED_BY_CREDENTIAL` | no key configured |
| MiniMax | `BLOCKED_BY_CREDENTIAL` | no key configured |

`BLOCKED_BY_CREDENTIAL` is **not a pass.** It means nothing ran.

Every provider passes the offline contract suite
([`tests/contract/test_provider_contract.py`](../tests/contract/test_provider_contract.py)),
parameterized across all nine so their answers cannot diverge.
