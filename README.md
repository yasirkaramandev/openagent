# OpenAgent

[![CI](https://github.com/yasirkaramandev/openagent/actions/workflows/ci.yml/badge.svg)](https://github.com/yasirkaramandev/openagent/actions/workflows/ci.yml)

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

We try to be precise about what is proven vs. pending, so nothing here is oversold. The labels mean:
**Verified live** (run against the real thing), **Fixture validated** (mapped from a recorded real
capture), **Offline contract tested** (mocked transport), **Experimental**, **Unsupported**.

| Area | State |
|---|---|
| **Codex CLI** | **Verified live** against `codex-cli 0.142.5`, end to end *through OpenAgent* — not just at the CLI. Captured from real runs: reasoning summaries, the `todo_list` plan (projected onto one checklist), `command_execution` with output, `file_change` (add/update/delete), `web_search`, usage incl. `reasoning_output_tokens`, cancellation (process tree terminated, status `cancelled`, no later `completed`), resume (turn 2 in the same thread), and a real failure (normalized `schema_mismatch`). Fixtures in `tests/fixtures/codex_v0142_*.jsonl` are sanitized captures of those runs. |
| **Antigravity (`agy`)** | **Verified live (read-only)**: the `--print --output-format json` envelope and `--conversation` resume were captured from `agy v1.1.0`, and re-exercised live on `agy v1.1.1` through OpenAgent in the [multi-agent demo](docs/multi-agent-weather-demo.md) (real runs, terminal states, `reasoning_tokens` from `thinking_tokens`). Model discovery is live via `agy models`. **Editing is experimental and off by default** — see [Antigravity permissions](#antigravity-permissions). Output is a single final JSON object, so events are **coarse** (final text + usage + status), never per-file/per-command. |
| **Claude Code** | **Fixture validated** — the `stream-json` mapping and invocation are ready, but `claude` is not installed on this machine, so nothing here has been run against a live CLI. |
| API agents (OpenAI Chat/Responses, Anthropic, OpenAI-compatible) | **Offline contract tested** end to end (mocked HTTP): tool loop, progress tools, cancellation, worktree diff, artifacts, redaction. **Not live-verified** against a paid key. Presets for DeepSeek/Qwen/Kimi/GLM/MiniMax/OpenRouter/Mistral/Together/Fireworks/Ollama/LM Studio share the compatible adapters and are **not individually live-verified**. |
| **Run Console** (live reasoning/plan/commands/files/diff/tests/usage/raw events) | Pilot-tested on Textual 8.2.8 at 80×24, 100×30 and 120×40; the live-run, leave-and-reopen, cancel and resume paths are driven end to end with a real subprocess. The Codex side of it is the live verification above. |
| TUI Add-Agent **wizard** | Pilot-tested with **real keyboard input** (Space selects, Enter advances): CLI and API paths, masked key cleared on every transition, connections filtered to the provider family, credentials validated on the connection step, `max_steps` bounded. **Model discovery** per backend — API providers via their models endpoint, Antigravity via `agy models`; CLIs that can't list models (Codex/Claude) fall back honestly to a manual id or the CLI's own default. |
| Security (minimal env, command allowlist, worktree/copy/in-place isolation, redaction, process-tree cancel, PID-identity recovery, sandboxed credential commands, streaming output bound, exact keychain rollback) | Unit + integration tested (see `tests/`). |
| **OS-level sandbox** | **Unsupported.** OpenAgent isolates by *workspace* (git worktree / copy), not by kernel sandbox. A CLI backend may bring its own (Codex `--sandbox`), and OpenAgent maps profiles onto it — but OpenAgent itself does not sandbox processes. |
| **Gemini** | **Not part of v0.1.** |

Everything except the live-CLI/live-API rows runs in the **offline test suite in CI**
(Ubuntu 3.10/3.11/3.12) with no API keys and no installed CLIs.

## Quick Install

**No pre-installed Python required.** The installer sets up an isolated Python runtime for OpenAgent
via [uv](https://docs.astral.sh/uv/) — it never touches your system Python and never creates a
`.venv` in the repo.

### macOS and Linux

```bash
git clone https://github.com/yasirkaramandev/openagent.git
cd openagent
bash setup.sh
```

After it finishes:

```bash
openagent
```

### Windows

```bat
git clone https://github.com/yasirkaramandev/openagent.git
cd openagent
setup.bat
```

After it finishes:

```bat
openagent
```

**What the installer does — and does not do:**

- You do **not** need Python installed first: the script installs a **managed Python 3.12** in an
  isolated location, just for OpenAgent.
- You never activate a `.venv`. OpenAgent is installed as an isolated `uv` tool and linked onto your
  PATH.
- **Nothing** is installed into your system Python, and no `.venv` is created inside the repo.
- **Re-running the script updates** your install; your agents, providers, runs, and keychain entries
  are preserved.
- OpenAgent **opens automatically** at the end of a normal install.
- In a **new terminal**, `openagent` works directly (the installer puts it on your PATH).

CI/automation can set `OPENAGENT_SETUP_NO_LAUNCH=1` to verify the install without opening the TUI.

### Updating

macOS/Linux:

```bash
git pull
bash setup.sh
```

Windows:

```bat
git pull
setup.bat
```

### Developer install (contributors only)

This is **not** the end-user install. To hack on OpenAgent itself, use a manual editable environment
with the dev tools:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

### Troubleshooting

- **`uv` download failed / corporate proxy or certificate** — the installer fetches `uv` over HTTPS
  from `astral.sh`. Behind a proxy, set `HTTPS_PROXY` / `SSL_CERT_FILE`, or install `uv` manually
  (<https://docs.astral.sh/uv/>) and re-run the script (it reuses an existing `uv`).
- **`openagent: command not found` after install** — open a **new** terminal; PATH changes apply to
  new shells. The installer also prints the exact executable path it linked.
- **`permission denied` running setup.sh** — run it as `bash setup.sh` (no execute bit needed), or
  `chmod +x setup.sh` first.
- **Windows execution/path issue** — run `setup.bat` from CMD, or `.\setup.bat` from PowerShell; paths
  with spaces are handled.
- **A different `openagent` is already on PATH** — the installer detects and reports the existing
  command, and prints the path of the one it just installed.
- **`doctor` warns about missing Codex/Claude/agy** — those are **optional** CLIs; their absence is a
  warning, **not** an install failure.

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

## AI Agent Skill

AI assistants can learn OpenAgent's safe CLI workflow from a bundled **skill**:

- [OpenAgent skill](skills/openagent/SKILL.md)
- [Skills index](skills/README.md)

The skill covers agent/provider setup, model selection, run execution, artifact inspection,
cancellation, resume, and the security boundaries an AI must respect (never put keys in `argv`, default
to `--worktree auto`, trust `status` not exit text, treat `failed`/`cancelled`/`orphaned` as *not*
completed, inspect the diff before accepting a change). Every command in it is accepted by the current
`openagent` CLI.

## Example: a real multi-agent build

[`examples/weather-map-app`](examples/weather-map-app/) is a working, no-API-key weather map built
as an end-to-end multi-agent demonstration: three `agy` agents (data / UI / QA) were created and run
**through OpenAgent**, the QA agent found a real bug, and a revision round fixed it — all with real
run IDs and terminal states. See [docs/multi-agent-weather-demo.md](docs/multi-agent-weather-demo.md).

## The Run Console

Running an agent opens a two-stage screen.

**Setup + preflight.** Pick the agent, the task, the workspace strategy and the permission profile.
The screen shows what that agent actually *is* — runtime, executable, detected version, auth state —
and runs a readiness checklist before anything is created:

```
✓ Agent exists: codex-coder          ✓ Authentication detected: ~/.codex/auth.json present
✓ Permission profile: safe-edit      ✓ codex exec supports --json
✓ codex found: /usr/local/bin/codex  ✓ Sandbox 'workspace-write' supported
✓ Version detected: codex-cli 0.142.5
```

`Run Agent` runs preflight itself, so a run never starts on an unready agent. A failed mandatory
check blocks it and says why.

**Live console.** A status header, tabbed panels — Overview, Reasoning, Plan, Commands, Files, Diff,
Tests, Messages, Usage, Raw Events — and a fixed action bar (Cancel · Follow-up · Back), all working
at 80×24. Every panel is *projected* from the event log, so an update replaces what it updates: the
plan is one checklist that ticks off, a command is one card whose output is its latest snapshot, a
file whose patch failed is red rather than green.

Closing the console does **not** stop the run. `Runs` shows phase, elapsed and current activity, and
Enter reopens the console — live for a running agent (replay the log, then tail it), replayed for a
finished one. `Follow-up` sends another turn into the same session.

### Reasoning summaries, not chain-of-thought

The Reasoning tab shows what the backend itself publishes for the user:

* **Codex** emits `reasoning` items — short summaries like `**Checking git status and file
  contents**`. That is what Codex designates as a user-visible summary, and OpenAgent asks for them
  explicitly (`model_reasoning_summary`), because without that Codex emits none at all.
* An **API agent** publishes its own via the `report_progress` and `update_plan` tools — explicit
  statements it chooses to make, not an extraction of anything hidden.

OpenAgent does **not** request, infer, store, or render private chain-of-thought. Reasoning *tokens*
are counted (`reasoning_tokens` in usage); the reasoning they represent is never obtained. When a
backend publishes no summaries, the tab says so and shows operational activity instead — it does not
invent a narrative.

## How it works

```
Interfaces:     TUI · CLI · (MCP, SDK — planned)
                        │
Services:       Agent · Provider · Model · Run · Preflight · Discovery · Doctor
                        │
Runtimes:       API agent loop (own tools)   │   CLI adapters (codex, claude, agy)
                        │
Workspace:      git worktree · permission profiles · command policy · secret redaction
                        │
Storage:        SQLite (index) · events.jsonl (source of truth) · projection · artifacts
```

Every run emits exactly one `run.started` (OpenAgent's — a backend process coming up is a separate
`process.started`), then `run.phase` transitions
(`preflight → preparing_workspace → starting_backend → running → finalizing`), then exactly one
terminal event per turn. `events.jsonl` is append-only; readers *project* it into current state keyed
by `(source, turn, item_id)`, which is what makes a run reopenable and what stops an updated plan
from becoming five plans.

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

### Antigravity permissions

Antigravity's `--print` mode is non-interactive, so it cannot answer its own tool-permission prompt.
The only way to let it edit is `--dangerously-skip-permissions`, which turns **Antigravity's own**
checks off — and OpenAgent cannot see Antigravity's internal tool calls to compensate. So v0.1 does
not infer that from a profile name:

| Profile | Antigravity behaviour |
|---|---|
| `read-only` | `--mode plan`. **Supported**, and the default. |
| `safe-edit` | Editing is **experimental and off**. Opt in with `OPENAGENT_ANTIGRAVITY_EXPERIMENTAL_EDIT=1`. |
| `development` / `full-access` | The native bypass is used only with `OPENAGENT_ANTIGRAVITY_DANGEROUS_BYPASS=1`, and the run emits a loud warning. |

A blocked combination fails at **preflight** with an actionable reason, rather than starting a run
that silently cannot do what was asked. `openagent doctor` reports the current state.

### Cancelling a run

Cancel really stops the agent, for both runtimes:

* **CLI** — the whole process tree is terminated (SIGTERM → SIGKILL), with PID+start-time identity
  verification so a reused PID is never killed.
* **API** — the run's cancellation flag is raised; the agent loop checks it before each provider
  request, on every stream chunk, around every tool call, and at the top of every step. The stream is
  abandoned (which closes the HTTP response), no further tools run, and the run **cannot** go on to
  report `completed`.
* **From a modal** — `Ctrl+C` in an approval or question dialog cancels the *run*, not just the
  dialog. The flag is raised before the modal is released, so the unblocked worker finds the run
  already cancelled. (`Esc` in a question dialog still just *skips* the question.)

A cancelled run records exactly one `run.cancelled`, and its artifacts stay readable.

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
