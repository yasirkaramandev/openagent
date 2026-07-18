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
└── timeline.md    integrity.json
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
| **Run Console** (live reasoning/plan/commands/files/diff/tests/usage/raw events) | Pilot-tested on Textual 8.2.8 at 120×40, 100×30, 80×24, 70×20, 60×18, 50×14 and 40×12, including focus visibility, page/home/end, mouse wheel, resize, fixed actions, long modals and explicit follow-output behavior. |
| TUI Add-Agent **wizard** | Pilot-tested with **real keyboard input** (Space selects, Enter advances): CLI and API paths, masked key cleared on every transition, connections filtered to the provider family, credentials validated on the connection step, `max_steps` bounded. **Model discovery** is source-specific: installed Codex app-server `model/list`, Claude config/aliases (not an entitlement claim), Anthropic `/v1/models` in an API-key context, and `agy models` in the current account context. Manual/default remains explicit when no reliable catalog exists. |
| Security (minimal env, command allowlist, worktree/copy/in-place isolation, redaction, process-tree cancel, PID-identity recovery, sandboxed credential commands, streaming output bound, exact keychain rollback) | Unit + integration tested (see `tests/`). |
| **Execution isolation** | `host-restricted` is a policy/approval boundary, **not** an OS sandbox. The opt-in `container-sandbox` gives API-agent tool commands a non-root, no-network, read-only-root Docker/Podman container with default seccomp, private PID/IPC, tmpfs workspace/resource limits and no host mount. The real-container contract is exercised in the dedicated Docker CI job. Long-lived CLI adapters are refused rather than silently falling back to the host. |
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

Or from PowerShell:

```powershell
git clone https://github.com/yasirkaramandev/openagent.git
cd openagent
.\setup.ps1
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

The normal update path is source-aware and verifies the exact active executable afterward:

```bash
openagent update --check       # network check only; no mutation
openagent update --dry-run     # show the exact source-matched command(s)
openagent update               # interactive confirmation
openagent update --yes --json  # automation; structured result
```

An install created by `setup.sh` / `setup.ps1` / `setup.bat` is tied to its local checkout.
`openagent update` updates it only when that checkout is clean, on `main`, and has the official
OpenAgent Git origin; it then fast-forwards with `git pull --ff-only` and re-runs the platform
installer with TUI launch disabled. Index installs use their owning `uv tool`, `pipx`, or exact
environment Python. Remote direct-URL installs and ambiguous provenance fail closed. The command
never chooses a different package manager merely because one happens to be on PATH.

After mutation it verifies all of the following before reporting success:

- PATH still resolves the same active `openagent` executable;
- the executable reports the expected version;
- source checkouts reached the previously verified `origin/main` revision;
- `openagent doctor --json` returns healthy (`0`) or warnings-only (`1`).

You can still update a checkout manually:

```bash
git pull
bash setup.sh                  # Windows: setup.bat or .\setup.ps1
```

The installers verify the source version, exact PATH winner, parseable Doctor JSON, and migration
health. They print any online-backup path. Doctor exit `2`, `3`, or `4` is a hard install failure and
the TUI is not launched; missing optional coding CLIs normally produce exit `1` and remain a warning.

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
- **Windows execution/path issue** — run `setup.bat` from CMD, `.\setup.bat` from PowerShell, or use
  the native `.\setup.ps1`; paths with spaces are handled.
- **A different `openagent` is already on PATH** — the installer prepends its tool directory and
  verifies the winner. If a machine-wide copy still shadows it, installation fails with both paths;
  remove or move the old copy and re-run setup. A shadow is never accepted as a warning-only result.
- **`doctor` warns about missing Codex/Claude/agy** — those are **optional** CLIs; their absence is a
  warning, **not** an install failure.
- **Doctor exits `2` or `3` during install/update** — `2` means the database is incompatible,
  corrupt, or contains invalid current-domain JSON; `3` means a pending migration failed and was
  rolled back. Preserve the backup path printed by the installer. Exit `4` means event-store
  integrity needs repair/investigation. None of these launches the TUI automatically.

## Quickstart

```bash
# Open the full-screen TUI
openagent

# Or drive it from the CLI:
openagent init                       # set up local state
openagent discover                   # detect installed coding CLIs (codex, claude, …)
openagent doctor                     # system diagnostics
openagent update --check             # check OpenAgent itself for an update
openagent cli list --json            # exact CLI paths, sources, conflicts, cached update state
openagent cli check --refresh --json # refresh official update metadata explicitly

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

An unverified model override always needs an auditable reason:

```bash
openagent add --name experimental --provider deepseek-main --model <model-id> \
  --allow-unverified-model --model-override-reason "manual compatibility review"
```

### Coding CLI discovery and update policy

OpenAgent treats the active executable and its installation source as security-relevant facts:

```bash
openagent cli list --json
openagent cli check --refresh --json
openagent cli update codex --dry-run
openagent cli update codex
openagent cli update --all --yes --json
```

`cli list` and the TUI show the active path, resolved realpath, source, version, update state, and
shadowed installations. An update uses only the updater matched to proven provenance (for example
npm, Homebrew cask, WinGet, or a documented native updater), runs with bounded output/time and a
credential-free environment, and verifies the exact executable afterward. Unknown sources,
multiple independent copies, unwritable targets, active runs, and package-manager operations that
would require elevation are blocked.

The update policy is stored under `cli_updates` in the `config.json` directory printed by
`openagent init`:

```json
{
  "cli_updates": {
    "policy": "ask",
    "check_interval_hours": 6,
    "check_before_run": true
  }
}
```

Policies are `notify`, `ask`, `auto`, and `never`. Metadata is cached; Doctor and normal TUI startup
remain offline unless refresh is explicit. `auto` still obeys every provenance/conflict/live-run
guard and never invokes `sudo`. In non-interactive environments an explicit `--yes` is required for
mutating all-install updates.

### Doctor exit codes

`openagent doctor --json` always includes the same numeric `exit_code` as the process:

| Code | Meaning |
|---:|---|
| `0` | All checks healthy. |
| `1` | Advisory warnings, commonly a missing optional Codex/Claude/agy CLI. |
| `2` | Core database/schema/domain incompatibility; do not launch or write further. |
| `3` | Pending migration failed and rolled back; preserve the reported online backup. |
| `4` | Event-store terminal/sequence/export integrity failure; inspect and repair before release work. |

### v0.1.4 lifecycle, discovery, and integrity boundaries

- **Project scope:** `.openagent/project.json` gives a project a stable UUID. Run listing, replay,
  output, cancel, resume and orphan recovery default to the active project; cross-project operations
  need explicit `--all-projects`. Use `openagent project list` and `openagent project relocate` for
  moved or missing roots.
- **Authoritative events and live monitoring:** full event bodies and sequence allocation live in
  SQLite in one write transaction. Run Console replays once and then polls only rows after its last
  sequence, so another OpenAgent process appears live without repeatedly reading the full history or
  applying duplicates. `events.jsonl` is an atomic export updated on its first event, batch boundary,
  terminal event, explicit flush, and shutdown; it is not the live source of truth and has no fixed
  250 ms freshness promise. `openagent events repair` regenerates it.
- **Migrations:** revisions are immutable and forward-only. Upgrades use `BEGIN IMMEDIATE`, create an
  online SQLite backup, then run integrity, foreign-key, row-ID/count, schema-parity and current
  Pydantic-domain validation across every JSON aggregate. The `0008`–`0011` revisions add real run
  foreign keys/turn leases, revision-consistent run payloads, exact legacy NVIDIA Build
  normalization, and domain validation. The whole pending chain is atomic; failure exits `3`, rolls
  back every pending revision, and reports the backup. Current-schema corruption exits `2`.
- **CLI lifecycle:** locator discovery enumerates PATH/native/npm/Homebrew/WinGet/legacy candidates,
  resolves safe executable realpaths, records the actual PATH winner and every independent shadow,
  and blocks update when provenance is unknown, another copy conflicts, or a live run uses the CLI.
  Update checks are cached and offline by default; only `--refresh` performs network metadata calls.
- **Model discovery:** Codex models are the catalog advertised by the installed Codex app-server's
  `model/list`. Claude subscription/OAuth has no public scriptable entitlement-list command, so
  OpenAgent exposes configured model names and aliases without calling them entitlement-verified;
  an Anthropic `/v1/models` result is verified only in that API-key context. Antigravity models come
  from `agy models` in the current signed-in account context. Partial catalogs remain partial and a
  structured unauthorized/rate-limit/timeout/network/malformed error is never confused with a valid
  empty catalog.
- **Model probes:** verification is keyed by provider/model/endpoint/protocol/opaque credential
  revision/probe version and expires after 24 hours. Catalog membership is not capability evidence;
  expiry, key rotation or provider changes invalidate the verdict.
- **Execution backends:** `host-restricted` blocks automatic interpreter/general-shell execution and
  approval-gates unsafe paths/operators, but it is not a kernel sandbox. `container-sandbox`
  requires an explicitly named, already-local Linux image with `/bin/sh` and Docker or Podman. It
  never pulls/builds, mounts a host path or falls back to the host; it rejects `--worktree none` and
  applies network `none`, a read-only root, UID/GID `65532:65532`, default seccomp, private PID/IPC
  namespaces, dropped capabilities, no-new-privileges, 2 CPU, 2 GiB memory/swap, 256 PID, 1 GiB
  workspace and 256 MiB `/tmp` limits. Sync-back first verifies that no host file changed
  concurrently, accepts only regular files, and preserves executable bits; timeout/cancel always
  removes the container. This backend is for API-agent tool commands; long-lived CLI adapters are
  refused under it.
- **Git/index integrity:** diff/status reads use NUL-delimited porcelain without mutating the user's
  index. Cleanup only removes worktrees/branches carrying OpenAgent ownership metadata. Optional
  agent commits require a clean OpenAgent-created worktree; in-place user changes are never
  committed. `openagent revert --id <run-id>` creates a revert commit.
- **Orphans and reruns:** cancelled/orphaned runs are terminal and never resume under the same ID.
  `openagent rerun --id <run-id>` allocates a new run. Cross-process cancellation changes state only
  after PID/create-time/executable/command identity proves the process tree terminated.
- **Secrets and TUI:** exact secrets are run-scoped and reference-counted; display sanitization is
  control removal → redaction → byte limit → single-line normalization → markup escaping. Password
  widgets are wiped on source/provider changes, success, failure, cancellation, unmount and worker
  termination. Responsive screens keep a scroll body and fixed actions down to 40×12.

## NVIDIA Build

[NVIDIA Build](https://build.nvidia.com/) is NVIDIA's **hosted catalog of NIM APIs**: one NVIDIA API
key reaches the models it publishes, over the OpenAI Chat Completions protocol at
`https://integrate.api.nvidia.com/v1`. (Self-hosting NIM yourself? Use the `custom`
OpenAI-compatible provider instead — this preset is for the hosted service.)

**Get an API key**

1. Sign in to [build.nvidia.com](https://build.nvidia.com/).
2. Open a model page.
3. Click **Generate API Key** / **Get API Key**.
4. Paste it into the hidden prompt below, or save it as `NVIDIA_API_KEY`.
5. **Never put the key directly in a command** — it would land in your shell history and CI logs.

Keys commonly begin with `nvapi-`, but that is only a hint: OpenAgent does not enforce the format.

**Connect it** — the OS keychain is the recommended method (the key is prompted with hidden input and
never becomes a command argument):

```bash
openagent provider add nvidia-build --type nvidia-build
```

Or reference an environment variable instead of storing a secret:

```bash
export NVIDIA_API_KEY="…"          # PowerShell: $env:NVIDIA_API_KEY="…"
openagent provider add nvidia-build --type nvidia-build --key-env NVIDIA_API_KEY
```

**Browse the catalog, then validate a model.** NVIDIA's catalog contains chat, embedding, reranking,
vision and other model types — *a catalog entry is not automatically compatible with OpenAgent
agents*, and reaching `/models` does not even prove your key works (the catalog may be public). So
`provider models` reports `capabilities: null` for every entry, and only `provider probe` validates:

```bash
openagent provider models nvidia-build --search nemotron
openagent provider models nvidia-build --owner nvidia --json
openagent provider probe nvidia-build --model nvidia/nemotron-3-ultra-550b-a55b
```

The probe really exercises the model — text, streaming, **and tool calling** — and claims only what it
observed. An OpenAgent API agent needs all three; a model that answers questions but cannot call tools
cannot operate OpenAgent's tools, and the probe says so instead of pretending:

```bash
openagent provider probe nvidia-build --model <publisher/model> --json
# {"provider":"nvidia-build","model":"…","text":true,"streaming":true,
#  "tool_calling":true,"agent_compatible":true,"category":"verified","tested_at":"…"}
```

`openagent provider test nvidia-build` only checks that the catalog is reachable; it never claims the
key is valid. Pass `--model <id>` to make it run a real probe.

**Create an agent and run it.** Creating an agent on an unvalidated catalog model is refused — pass
`--allow-unverified-model` plus `--model-override-reason` to override explicitly (the agent is then
flagged as overridden, never Verified):

```bash
openagent add \
  --name nvidia-coder \
  --provider nvidia-build \
  --model nvidia/nemotron-3-ultra-550b-a55b \
  --profile safe-edit

openagent run --name nvidia-coder --worktree auto \
  --prompt "Inspect this repository and summarize its structure."

openagent output --id <run-id> --format json
openagent output --id <run-id> --format events
```

If you intentionally skip/override validation, add both
`--allow-unverified-model --model-override-reason "…"` to the `openagent add` command.

> The model id above is an example. Catalogs rotate — list the catalog and probe a current model
> rather than trusting a hardcoded id.

**Privacy:** some NVIDIA models stream a `reasoning_content` field. That is raw chain-of-thought, and
OpenAgent never stores or displays it — not in `events.jsonl`, `timeline.md`, `result.json`, or the
UI. Only the final answer is kept (and, if reported, the numeric reasoning-token count). Your API key
is likewise scrubbed from every artifact and log.

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
Storage:        SQLite (authoritative events + sequence) · JSONL export · projection · artifacts
```

Every run emits exactly one `run.started` (OpenAgent's — a backend process coming up is a separate
`process.started`), then `run.phase` transitions
(`preflight → preparing_workspace → starting_backend → running → finalizing`), then exactly one
terminal event per turn. SQLite is authoritative; an atomic `events.jsonl` export mirrors it. Readers
*project* the database stream into current state keyed by `(source, turn, item_id)`, which is what
makes a run reopenable and what stops an updated plan
from becoming five plans.

* **Providers vs. Agents.** A *provider* is an API account (no prompt, no role). An *agent* binds a
  runtime + prompt + tags + permission profile. Many agents can share one provider; the key is
  stored once.
* **Dynamic models.** Model IDs are never hardcoded — OpenAgent discovers them and probes
  capabilities per model.
* **Safety first, with explicit boundaries.** Every file-changing run happens in an isolated
  worktree/copy, so your real project is untouched until you apply. Commands run in a minimal
  environment (no inherited secrets) behind an executable **allowlist** with approval gating; secrets
  are redacted from every artifact — including the prompt and the diff — and never passed as command
  arguments. `host-restricted` remains a policy layer; the opt-in container backend provides an OS
  boundary for API-agent tool commands. See [SECURITY.md](SECURITY.md) for the exact boundary.

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
.venv/bin/ruff format --check .
.venv/bin/ruff check src tests
.venv/bin/mypy src
.venv/bin/python -m pytest -q
.venv/bin/python -m build
```

The same checks run in [GitHub Actions](.github/workflows/ci.yml) on every push and pull request:
`ruff` + `mypy` + the full offline `pytest` suite on Ubuntu (Python 3.10 / 3.11 / 3.12), a package
build, and a clean-venv wheel-install + entrypoint check, plus cross-platform smoke jobs on macOS and
Windows, real Unix/Windows installers, a v0.1.2 wheel/DB upgrade-and-backup-restore job, and a real
Docker sandbox job. The offline suite requires **no API keys and no installed CLIs** — CLI runs are exercised
with a real-subprocess fake, and providers with mocked HTTP.

Optional, non-inference live CLI checks are explicitly opt-in. They run version probes, Codex
app-server model discovery, `agy models`, and Claude Doctor only for CLIs installed on the machine:

```bash
OPENAGENT_LIVE_CLI_TESTS=1 .venv/bin/python -m pytest -q -m live_cli tests/live
```

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
