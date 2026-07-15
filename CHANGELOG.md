# Changelog

All notable changes to OpenAgent are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and this project aims to follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] — 2026-07-15

First tagged release (alpha). Local-first control plane for AI APIs, coding CLIs, and agents.

### Added
- **TUI + CLI** control plane: register agents (API or CLI), run tasks in isolated
  worktree/copy/in-place workspaces, follow a live **Run Console**, cancel, follow-up, and reopen.
- **API agents** with a tool loop over OpenAI (Chat + Responses), Anthropic, and generic
  OpenAI-compatible providers; presets for DeepSeek/Qwen/Kimi/GLM/MiniMax/OpenRouter/Mistral/
  Together/Fireworks/Ollama/LM Studio.
- **CLI adapters**: Codex (verified live), Antigravity/`agy` (verified live, read-only), Claude Code
  (fixture validated).
- **Dynamic model discovery** in the Add-Agent wizard: API providers via their models endpoint,
  Antigravity via `agy models`; CLIs without a listing command fall back to a manual id or the CLI's
  own default (never a fabricated list).
- OS-keychain credentials, minimal child environment, command allowlist, secret redaction across
  events/artifacts, and process-tree cancellation.
- `OPENAGENT.md` generation, the standard run artifact bundle (events/result/timeline/diff), and a
  worked **[multi-agent demo](docs/multi-agent-weather-demo.md)** (`examples/weather-map-app`).

### Fixed
- **Run lifecycle (P0):** the terminal event (`run.completed`/`failed`/`cancelled`) is now the
  **last** entry in the event log. Previously every CLI run logged the terminal event mid-stream and
  then wrote `run.phase(finalizing)` after it, leaving a projection that read "completed / finalizing"
  — the state the TUI must never show. The terminal event is buffered and written last, after
  finalizing + diff; a finalization error invalidates a buffered success without masking an earlier
  failure.
- **`openagent output --format json`** emitted invalid JSON when piped, because Rich soft-wrapped the
  string at the console width. Artifacts are now written verbatim — the documented
  `openagent output --format json` call parses correctly.
- **Antigravity usage:** `thinking_tokens` is normalized to `reasoning_tokens`, matching every other
  backend. Adapter capabilities no longer advertise experimental, permission-bypassing editing as a
  normal, verified feature.

### Known limitations
- API-provider presets and Claude Code are not individually verified against live keys/CLIs.
- Antigravity file-editing requires an explicit, experimental opt-in (`--dangerously-skip-permissions`
  disables its own tool checks); it is off by default.
- No OS-level/kernel sandbox — isolation is by workspace, not by process.
- `agy` plan-mode reviews can exceed its print timeout on large multi-file prompts.

[0.1.0]: https://github.com/yasirkaramandev/openagent/releases/tag/v0.1.0
