---
name: openagent
description: Use OpenAgent to discover runtimes, create API or CLI agents, execute and observe runs, inspect artifacts, cancel safely, and continue resumable sessions.
---

# OpenAgent skill

OpenAgent is a local-first control plane for AI APIs, coding CLIs (Codex, Claude Code, Antigravity),
and autonomous agents. This skill teaches an AI assistant how to drive its **CLI** correctly and
safely. Every command below is accepted by the real `openagent` CLI; keep this file in step with
`openagent --help`.

## Purpose

Give an AI assistant a reliable, machine-readable workflow: discover what is installed, create
agents, run tasks in an isolated workspace, and read structured results — without guessing at state
or leaking secrets.

## When to use OpenAgent

Use it when the user wants to delegate a coding task to an installed coding CLI or an API model under
OpenAgent's supervision (isolated workspace, recorded events, reviewable diff), or to inspect/continue
a previous run. Do not use it to run arbitrary shell for its own sake — it is an agent control plane.

## Mandatory start-up flow

Always begin by reading state. Do not assume anything is installed or configured.

```bash
openagent version
openagent doctor --json
openagent discover
openagent agent list --json
openagent provider list --json
```

- `version` confirms the CLI is on PATH and which build you are driving.
- `doctor --json` is the health snapshot. A **non-zero** exit only because optional CLIs
  (Codex/Claude/agy) are missing is **not** fatal — read the JSON and decide. A broken OpenAgent
  install (import/entrypoint error) is fatal.
- `discover` detects installed coding CLIs and whether they are authenticated.
- `agent list --json` / `provider list --json` show what already exists — reuse before creating.

## Prerequisite checks

Before creating an agent, confirm the backend it needs exists:

- CLI agent → the CLI must appear in `openagent discover` as found (and usually authenticated).
- API agent → a matching provider must appear in `openagent provider list --json`.

## Discovering installed CLIs

```bash
openagent discover                 # human-readable
openagent agent list --json        # existing agents (machine-readable)
```

If a CLI is not installed, do **not** invent one — tell the user, or pick a CLI that `discover`
reports as available.

## Inspecting existing agents / providers

```bash
openagent agent list --json
openagent agent show <name>        # full JSON for one agent (runtime.model, profile, …)
openagent provider list --json
```

## Creating CLI agents

```bash
openagent add \
  --name codex-coder \
  --cli codex \
  --model gpt-5.5 \
  --profile safe-edit
```

- `--model` is optional for CLI agents. Omit it to use the CLI's own default; **when you omit it,
  say so explicitly** — the run inherits whatever the CLI's global config names.
- A pinned `--model` makes the agent reproducible. Verify it was stored with `openagent agent show`.

Antigravity, read-only (review-only, no edits):

```bash
openagent add \
  --name agy-reviewer \
  --cli antigravity \
  --model "Gemini 3.5 Flash (Low)" \
  --profile read-only
```

## Creating API providers and agents

Never put an API key in `argv`. Add the provider first (the key is prompted with hidden input, or
referenced from an environment variable with `--key-env`):

```bash
openagent provider add deepseek-main --type deepseek --key-env DEEPSEEK_API_KEY
openagent provider test deepseek-main
openagent add --name ds-coder --provider deepseek-main --model deepseek-chat --profile safe-edit
```

## Choosing models

- CLI agents: some CLIs enumerate models (e.g. `agy models` via Antigravity); Codex/Claude expose
  `--model` but no listing — do not fabricate a list.
- API agents: `openagent provider models <name>` lists what the connection reports (best-effort).
- A manually typed model id is **not verified**. State that when you use one.
- Using the CLI's default model (`--model` omitted) persists as "no pinned model" — call it out.

## Running tasks

```bash
openagent run \
  --name codex-coder \
  --worktree auto \
  --prompt "Inspect the repository, implement the requested fix, and run tests."
```

Record the printed **run id**. The text the command prints on return is a summary, not the source of
truth — always read the machine-readable result (below).

## Choosing workspace strategy

- Default to `--worktree auto` (isolated git worktree / copy; the user reviews the diff).
- `--worktree none` runs **in place** in the user's project with no isolation. Use it only when the
  user has explicitly approved editing their working tree directly.

## Reading machine-readable results

```bash
openagent output --id <run-id> --format json     # result.json (status, summary, usage, files)
openagent output --id <run-id> --format diff      # changes.diff
openagent output --id <run-id> --format events    # events.jsonl (full event stream)
openagent output --id <run-id> --format tests     # tests.json
```

Parse `--format json`; do not scrape the human summary.

## Checking terminal states

A run is only done when its `status` is terminal. Treat these distinctly — **none of them is
"completed"**:

- `completed` — success.
- `failed` — it did not finish; read `failure_type` and the failure section.
- `cancelled` — stopped by a user/cancel.
- `orphaned` — OpenAgent lost ownership/observation of the run. **The underlying process may be
  gone, reused, unverifiable, or still alive but unattached.** Inspect `failure_type` before acting.

`orphaned` does **not** mean "the process is gone". Read `failure_type`:

| `failure_type` | What it means | Is a process still running? |
| --- | --- | --- |
| `orphaned_pid_gone` | no such PID any more | no |
| `orphaned_pid_reused` | that PID now belongs to an **unrelated** process | not ours — never touch it |
| `orphaned_pid_unknown` | the PID is live but identity cannot be verified | unknown — fail closed |
| `orphaned_unattached_process` | the backend process is **still alive**, just unowned | yes — it may still be running |

If `status` is not terminal, the run is still going — do not assume success.

## Cancelling runs

```bash
openagent cancel --id <run-id>
```

Cancellation is real: it tears down the provider stream / kills the CLI process tree and records a
single `run.cancelled` as the last event. Confirm with `openagent output --id <run-id> --format json`.

It also works on an **orphaned** run:

- `orphaned_unattached_process` may still be alive — `openagent cancel --id <run-id>` performs a
  **PID + create-time identity verification** and only then terminates the process tree.
- Any other orphan reason (gone / reused / unknown) is refused: the command exits non-zero and kills
  nothing, because the recorded PID cannot be safely tied to this run.
- **Never kill a PID manually without identity verification.** PIDs are reused; `kill <pid>` from a
  run record can terminate an unrelated process. Always go through `openagent cancel`.

`openagent cancel` never prints a false success. Trust its exit code and message:
`terminated` / `signalled` (something was actually stopped), `already_terminal` (nothing to do),
`not_found`, `identity_mismatch` or `not_cancellable` (nothing was stopped — non-zero exit).

## Following up / resuming

```bash
openagent message --id <run-id> --prompt "Also update the changelog."
```

- Resume/follow-up is supported for CLI backends that reported a session id. Check
  `openagent output --id <run-id> --format json` (or `agent show`) before assuming it is available.
- Never send a follow-up to a run whose current turn has not finished, and never send two concurrent
  follow-ups to the same run.

## Inspecting diffs and files

```bash
openagent output --id <run-id> --format diff
```

Review the diff before you tell the user the change is acceptable. Do not accept edits you have not
inspected.

## Handling failures

- `failed` → read `failure_type` in `result.json`; surface the safe message, do not retry blindly.
- `orphaned` → OpenAgent lost track of the run; do not claim it completed. Read `failure_type`: with
  `orphaned_unattached_process` the process may still be running — stop it with
  `openagent cancel --id <run-id>` (never with a manual `kill`). Start a fresh run if needed.
- `artifacts_partial: true` in `status.json`/`result.json` → the bundle was rebuilt by failure
  recovery and is incomplete; read `artifact_failure.stage`. Never report such a run as completed.
- Missing optional CLIs in `doctor` are warnings, not install failures.

## NVIDIA Build (hosted NIM APIs)

NVIDIA Build is a **hosted catalog** at `https://integrate.api.nvidia.com/v1` speaking OpenAI Chat
Completions. One NVIDIA API key reaches the catalog's models. The catalog mixes model *types*, so a
model id proves nothing until it is probed.

```bash
openagent provider list --json
openagent provider models nvidia-build --json
openagent provider probe nvidia-build --model <publisher/model> --json
openagent add --name <name> --provider nvidia-build --model <publisher/model>
```

Add the connection with a hidden prompt (never argv), or reference an env var:

```bash
openagent provider add nvidia-build --type nvidia-build            # prompts for the key
openagent provider add nvidia-build --type nvidia-build --key-env NVIDIA_API_KEY
```

Read the probe JSON, not prose:

```json
{"provider": "nvidia-build", "model": "…", "text": true, "streaming": true,
 "tool_calling": true, "agent_compatible": true, "category": "verified", "tested_at": "…"}
```

Only `agent_compatible: true` (category `verified`) means the model can run an OpenAgent agent.

Hard requirements:

- **Never put `NVIDIA_API_KEY` (or any key) in argv.** Use the hidden prompt or `--key-env`.
- **Never assume every NVIDIA catalog model is a chat model.** `provider models` reports
  `capabilities: null` for every entry — that is the truth, not a gap to fill in.
- **Probe before creating a normal agent.** `openagent add` refuses an unprobed mixed-catalog model
  unless `--allow-unverified-model --model-override-reason "…"` is passed. An override is never
  shown as Verified, and you must relay both the status and reason.
- Treat embedding / rerank / vision models as **unverified** until capability testing. Name-based
  guesses are hints only, never verdicts.
- `openagent provider test nvidia-build` only proves the **catalog is reachable** — it does not mean
  the key is valid. Do not report it as "authenticated". Use `provider probe` to validate.
- **Never expose `reasoning_content`.** OpenAgent never stores it; do not try to surface it.
- Trust the machine-readable probe result and run status over any prose.

## Security rules (hard requirements)

- Never put an API key, token, or secret in `argv`, a prompt, or an artifact.
- Default to `--worktree auto`. Use `--worktree none` only with explicit user approval.
- Trust `status`, not the exit text. `failed`/`cancelled`/`orphaned` are never "completed".
- Save the run id; read results with `openagent output --id <run-id> --format json`.
- Inspect the diff before accepting a change.
- Check resume support before sending a message; never send concurrent follow-ups to one run.
- If you used the CLI's default model, say so. If you typed a model id manually, say it is unverified.
- Do not enable Antigravity's editing bypass automatically.
- Do not request or surface hidden chain-of-thought — use only reasoning summaries and operational
  events.

## Known limitations

- Resume/follow-up is CLI-only in v0.1.x and needs a reported session id.
- Model listing exists only for CLIs/providers that actually expose it.
- A restarted OpenAgent cannot reattach to a run started by a previous process — such runs are marked
  `orphaned` (fail-closed), not silently "completed". Their process may still be alive; stop it with
  `openagent cancel --id <run-id>`, which verifies PID + create-time identity first.
- NVIDIA Build's asynchronous (HTTP 202 + request id) model types are **not** supported by the chat
  runtime; they fail explicitly rather than returning an empty answer.
- Capability probes persist in SQLite for 24h. Provider/model/endpoint/protocol/credential revision
  or probe-version changes invalidate them fail-closed. If overriding, pass both
  `--allow-unverified-model` and `--model-override-reason`, and say so in your report.
- `host-restricted` is a policy boundary, not an OS sandbox. The opt-in container backend currently
  covers API-agent tool commands; it refuses long-lived CLI adapters instead of falling back to host.

## Complete example

```bash
# 1) See what's available.
openagent version
openagent doctor --json
openagent discover
openagent agent list --json

# 2) Create a CLI agent (model omitted -> CLI default; note that to the user).
openagent add --name codex-coder --cli codex --profile safe-edit
openagent agent show codex-coder

# 3) Run a task in an isolated workspace.
openagent run --name codex-coder --worktree auto \
  --prompt "Fix the failing test in tests/ and run pytest."
# -> note the run id, e.g. run_abc123

# 4) Read the structured result and the diff.
openagent output --id run_abc123 --format json
openagent output --id run_abc123 --format diff

# 5) Only if status == completed and the diff looks right, tell the user it's ready.
```
