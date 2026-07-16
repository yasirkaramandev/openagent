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

## v0.2 — Broader providers & CLIs

- Gemini CLI + Gemini API
- DeepSeek, Qwen, Kimi, GLM, MiniMax, OpenRouter, Ollama, LM Studio adapters wired end-to-end
- Full CLI discovery, richer doctor, model discovery + capability probe surfaced in the wizard
- Session resume across runtimes

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
