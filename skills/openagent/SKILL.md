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
- `orphaned` — the process was lost (e.g. across a restart) and could not be reattached.

If `status` is not terminal, the run is still going — do not assume success.

## Cancelling runs

```bash
openagent cancel --id <run-id>
```

Cancellation is real: it tears down the provider stream / kills the CLI process tree and records a
single `run.cancelled`. Confirm with `openagent output --id <run-id> --format json`.

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
- `orphaned` → the process is gone; do not claim it completed. Start a fresh run if needed.
- Missing optional CLIs in `doctor` are warnings, not install failures.

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
  `orphaned` (fail-closed), not silently "completed".

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
