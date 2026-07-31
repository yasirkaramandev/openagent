# Roadmap

OpenAgent's core problem is running many different AI backends **reliably, safely, and in a
standard shape**. Once that core is solid, auto-routing and comparison features can be added on top
of real usage data.

## v0.1 — Working core ✅

- `openagent` TUI + `openagent` CLI (init, add, agent/provider management, run, output, doctor)
- SQLite-authoritative events/projects/migrations, OS-keychain credentials, run IDs, atomic JSONL
  export, `output.md`, `result.json`, `timeline.md`, and artifact integrity hashes
- **Live Run Console**: readiness preflight, then reasoning summaries, plan, commands, files, diff,
  tests, messages, usage and raw events — closable and reopenable without stopping the run
- Isolated git worktrees + permission profiles; explicit confirmation before editing in place
- **Codex CLI** adapter — verified live end to end (reasoning, plan, commands, files, web search,
  cancel, resume, failure); **Antigravity** verified live read-only (editing experimental, opt-in);
  **Claude Code** fixture-validated
- **OpenAI** (Chat + Responses), **Anthropic** Messages, and generic **OpenAI-compatible** API agents,
  with user-visible `update_plan` / `report_progress` tools
- Real cancellation for both runtimes (process tree for CLI; loop checkpoints + stream teardown for
  API), including from an approval/question modal
- `OPENAGENT.md` generation; secret redaction; command policy; orphan recovery

### Known limits in v0.1

- `host-restricted` is not an OS sandbox. The opt-in container backend currently isolates structured
  API-agent tool commands; long-lived CLI-adapter container execution remains a known limit.
- **API agents are not live-verified** — offline contract tests only.
- **Claude Code is fixture-only** — not run against an installed `claude`.
- **Antigravity editing is experimental** and requires an explicit opt-in; its `--print` output is a
  single final object, so per-file/per-command events are not available.
- **Follow-up/resume is CLI-only**, and only between turns: a non-interactive CLI process cannot take
  new input mid-turn, and OpenAgent says so rather than pretending otherwise.

## v0.2 — Broader providers & CLIs 🚧

Implemented on `feat/v0.2-integration`. See [docs/providers.md](docs/providers.md),
[docs/cli-adapters.md](docs/cli-adapters.md), [docs/session-resume.md](docs/session-resume.md).

**Nine API providers**, as data over five shared protocol wires rather than nine adapters: Gemini
(Interactions), Ollama, LM Studio, OpenRouter, DeepSeek, Qwen, Kimi, GLM, MiniMax. Regions are
modelled as separate deployments, because a key is valid on exactly one of them.

**Six CLI adapters**: Codex, Claude Code, Gemini CLI (completed and registered), plus experimental
Qwen Code and Kimi ACP (JSON-RPC over stdio), and Antigravity.

**Capability evidence replaces capability booleans.** Every claim carries its source — live probe,
provider catalog, verified fixture, curated preset, manual override — its age, and the model revision
it was observed against. A probe that could not run leaves capabilities `UNKNOWN` rather than marking
them unsupported.

**Full CLI discovery** with one descriptor and one report shape per CLI, six honest status labels, and
a tested guarantee that a probe of one CLI never receives another provider's credential.

**Rich Doctor** — eleven sections, each carrying its own worst status.

**Add-Agent wizard v2** — evidence-sourced badges, four explicit catalog fallbacks, a probe gate that
actually gates, and server-state disclosure at the step where it is enabled.

**Cross-runtime session resume** — four named modes, with blockers and warnings kept apart, and a
turn terminal contract reconciled fail-closed.

### Not done in v0.2

- **Live verification is credential-blocked for eight of nine providers.** Ollama is live-verified in
  CI against a real daemon; the rest are `BLOCKED_BY_CREDENTIAL`, which is not a pass.
- **Gemini SSE framing** remains `SSE_FRAMING_LIVE_UNVERIFIED`.
- **Claude Code** is installed on the verification machine but signed out: `BLOCKED_BY_AUTH`, so its
  event mapping is still fixture-validated.
- **Gemini CLI headless resume is unsupported** — no documented machine-readable session contract.
- **Migrations 0015–0017** are written and tested but not chained, pending 0014 reaching `main` with
  the 0.1.6 release.

## v0.3 — Orchestration

- Antigravity (`agy`) experimental adapter (worktree-required, version-gated, PTY fallback)
- **MCP server** (`openagent mcp serve`) exposing agents to other AIs
- Workflow engine (`openagent workflow run ...`) with explicit step DAGs
- Approval UI, provider health screen, usage & cost tracking
- Plugin SDK (providers, CLI adapters, tools, reports)

## v0.4 — Wider ecosystem

- ByteDance Doubao / Volcano Ark, Baidu Qianfan, Mistral, Together, Fireworks, vLLM
- OpenCode / Qwen Code / Kimi Code via the generic manifest adapter
- Custom-command agents

## v1.0 — Stable

- Frozen provider + CLI-adapter contracts
- Migrations, security audit, comprehensive docs, stable workflow format

## Explicitly out of scope (for now)

Auto agent-selection, ML router, cloud control plane, team sync, marketplace, mobile/web dashboards,
remote/distributed execution, and automatic `git push`/deploys. These are revisited only after the
core is proven.
