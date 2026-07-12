# OpenAgent

[![CI](https://github.com/yasirkaramandev/open-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/yasirkaramandev/open-agent/actions/workflows/ci.yml)

**Local-first control plane for AI APIs, coding CLIs, and autonomous agents.**

> Register every AI agent once. Run all of them through one standard interface.

OpenAgent unifies two kinds of AI backends behind a single terminal UI, CLI, and (soon) MCP server:

1. **API agents** — OpenAI, Anthropic, DeepSeek, Qwen, Kimi, GLM, MiniMax, OpenRouter, Ollama, and
   any OpenAI-/Anthropic-compatible endpoint. These only emit text and tool calls, so OpenAgent runs
   its **own agent loop** with a safe toolset (read / search / patch / run / test).
2. **CLI agents** — Codex CLI, Claude Code, Antigravity (`agy`), and more. These have their own
   loops; OpenAgent runs them as subprocesses and **normalizes their output** into one event stream.

Whichever backend does the work, every run produces the **same standard artifact bundle**. Each run
executes in an isolated **Git worktree**, an isolated **directory copy** (non-git projects), or an
**explicitly confirmed in-place** workspace — that standardization is the point.

```
.openagent/runs/run_01ABC/
├── request.json   status.json   events.jsonl   output.md
├── result.json    logs.txt      changes.diff   tests.json   handoff.md
```

## Status

**v0.1 (alpha).** Working core: TUI + CLI, OpenAI (Chat + Responses) / Anthropic / generic
OpenAI-compatible API agents with a tool loop, CLI adapters (Codex, Claude Code, Antigravity),
worktree/copy/in-place isolation, permission profiles, OS-keychain credentials, and the standard run
bundle. See [ROADMAP.md](ROADMAP.md) for what's next.

### Maturity — what's actually verified

We try to be precise about what is proven vs. pending, so nothing here is oversold.

| Area | State |
|---|---|
| API agents (OpenAI Chat/Responses, Anthropic, OpenAI-compatible) | Adapter implemented; offline-tested end to end (mocked HTTP): tool loop, worktree diff, artifacts, redaction. **Live-unverified** — not yet exercised against a paid live key in CI. Presets for DeepSeek/Qwen/Kimi/GLM/MiniMax/OpenRouter/Mistral/Together/Fireworks/Ollama/LM Studio share the OpenAI/Anthropic-compatible adapters but are **not individually live-verified**. |
| **Codex CLI** | Event schema validated **live** against `codex-cli 0.142.5`; the full run/cancel/terminal-state pipeline is exercised via a real-subprocess fake-CLI harness. A **successful real model turn is pending account/usage-limit availability**. |
| **Claude Code** | **Fixture-validated** — the `stream-json` mapping and invocation are ready, but not yet run against an installed `claude` on this machine. Treat as unverified against a live CLI. |
| **Antigravity (`agy`)** | **Verified live** against `agy v1.1.0`: `--print --output-format json` result envelope and `--conversation` resume were captured from the real CLI (`tests/fixtures/antigravity_print.jsonl`), and the full OpenAgent→agy pipeline (session capture, usage, one terminal event) was run end to end. Output is a single final JSON object, so events are **coarse** (final text + usage + status), not per-file/per-command. Failure/cancel envelope shapes are inferred (fail-closed), not captured live. |
| TUI Add-Agent **wizard** | Backend-first multi-step wizard (Backend → CLI/Provider → Connection → Agent details) on **Textual 8.2.8**, pilot-tested with **real keyboard-driven** radio/list selection (not `.value` assignment): CLI path (Codex/Claude/Antigravity, incl. unavailable-CLI handling), API path (provider cards, new/existing connection, masked key, missing-key inline error, local no-key providers), Back/Continue navigation preserving non-secret input, and Cancel. A fixed action bar keeps Continue/Create visible; the API key uses `SecretStr` and never reaches persistence/logs. |
| TUI (dashboard, agents, providers, run, approvals, ask_user) | Pilot-tested against Textual 8.2.8: agent/provider screens, live run stream, approval modal, and the end-to-end `ask_user` question flow (modal → answer → next model request). Every `Select` empty state is normalised so no Textual sentinel reaches a service or model. |
| Security (minimal env, command allowlist, worktree/copy/in-place isolation, redaction, process-tree cancel, PID-identity recovery, sandboxed credential commands) | Unit + integration tested (see `tests/`). |
| **Gemini** | **Not part of v0.1.** |

Everything above except the live-CLI/live-API caveats runs in the **offline test suite in CI**
(Ubuntu 3.10/3.11/3.12) with no API keys and no installed CLIs.

## Install

Requires **Python 3.10+**. From a clone:

```bash
python3.10 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Quickstart

```bash
# Open the full-screen TUI
openagent

# Or drive it from the CLI:
openagent init                       # set up local state
openagent discover                   # detect installed coding CLIs (codex, claude, …)
openagent doctor                     # system diagnostics

# Register an installed CLI as an agent (codex | claude | antigravity)
openagent add --name codex-coder --title "Codex Coder" --cli codex --tag coder
openagent add --name antigravity-coder --title "Antigravity" --cli antigravity --tag coder

# Register an API provider (key is prompted with hidden input, stored in the OS keychain)
openagent provider add deepseek-main --type deepseek
openagent add --name deepseek-coder --provider deepseek-main --model <model-id> --tag backend

# Run a task in an isolated worktree, then read the result
#   --worktree auto|none|copy   (none needs -y to confirm running in place)
#   -y / --yes                   approve high-risk ops non-interactively (records approval events)
openagent run --name codex-coder --prompt "update the WSS client in main.py" --worktree auto
openagent output --id <run-id> --format md
openagent output --id <run-id> --format diff

# Continue the session with another turn; cancel a live run (terminates the process tree)
openagent message --id <run-id> -p "now add a test"
openagent cancel --id <run-id>
```

## How it works

```
Interfaces:     TUI · CLI · (MCP, SDK — planned)
                        │
Services:       Agent · Provider · Model · Run · Discovery · Doctor
                        │
Runtimes:       API agent loop (own tools)   │   CLI adapters (codex, claude)
                        │
Workspace:      git worktree · permission profiles · command policy · secret redaction
                        │
Storage:        SQLite (index) · events.jsonl (source of truth) · artifacts
```

* **Providers vs. Agents.** A *provider* is an API account (no prompt, no role). An *agent* binds a
  runtime + prompt + tags + permission profile. Many agents can share one provider; the key is
  stored once.
* **Dynamic models.** Model IDs are never hardcoded — OpenAgent discovers them and probes
  capabilities per model.
* **Safety first (policy-level, not an OS sandbox).** Every file-changing run happens in an isolated
  worktree/copy, so your real project is untouched until you apply. Commands run in a minimal
  environment (no inherited secrets) behind an executable **allowlist** with approval gating; secrets
  are redacted from every artifact — including the prompt and the diff — and never passed as command
  arguments. v0.1 does **not** add a kernel-level network/filesystem sandbox around subprocesses —
  see [SECURITY.md](SECURITY.md) for the exact boundary.

## Permission profiles

| Profile | Edits | Commands | Network commands | Codex sandbox |
|---|---|---|---|---|
| `read-only` | no | limited | approval-gated | `read-only` |
| `safe-edit` (default) | yes | tests/build | approval-gated | `workspace-write` |
| `development` | yes | yes | allowed | `workspace-write` |
| `full-access` | yes | yes | allowed | `danger-full-access` |

The **Network commands** column is a *policy/approval* boundary, **not** an OS-level network
sandbox: an "approval-gated" profile routes network-*oriented* commands (`curl`, `pip install`,
`git clone`, …) through an explicit approval — it does not block sockets at the kernel level. The
**Codex sandbox** column is the flag OpenAgent passes to the Codex CLI, which enforces its own
sandbox. See [SECURITY.md](SECURITY.md) for the full threat model.

## Security

See [SECURITY.md](SECURITY.md). Highlights: OS-keychain credentials, minimal subprocess
environments (no inherited secrets), worktree isolation, an executable **allowlist** with approval
gating, process-tree cancellation with PID-identity verification, and secret redaction across every
artifact (prompt and diff included).

## Development

```bash
.venv/bin/ruff check src tests
.venv/bin/mypy src
.venv/bin/python -m pytest -q
.venv/bin/python -m build
```

The same checks run in [GitHub Actions](.github/workflows/ci.yml) on every push and pull request:
`ruff` + `mypy` + the full offline `pytest` suite on Ubuntu (Python 3.10 / 3.11 / 3.12), a package
build, and a clean-venv wheel-install + entrypoint check, plus cross-platform smoke jobs on macOS and
Windows. The offline suite requires **no API keys and no installed CLIs** — CLI runs are exercised
with a real-subprocess fake, and providers with mocked HTTP.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
