# Architecture

A short map of the codebase. The guiding idea: **one normalized event stream and one artifact bundle**
regardless of which backend runs the work.

## Layers

```
Interfaces      tui/ (Textual)   ·   cli/ (Typer)        [MCP, SDK planned]
                        │
Services        services/  — the single business layer (agent, provider, model,
                             run, discovery, doctor). CLI + TUI both call these.
                        │
Runtimes        runtimes/api_agent/  — OpenAgent's own loop for API models
                runtimes/cli/        — subprocess adapters (codex, claude, generic)
                        │
Substrate       providers/  tools/  workspaces/  security/  credentials/
                        │
Storage         storage/ (SQLite index)  ·  events.jsonl (source of truth)  ·  reporting/
```

## Key contracts

- **`ProviderAdapter`** (`providers/base.py`): `test_connection`, `list_models`, `probe_model`,
  `stream_response`, `count_tokens`. The agent loop speaks only normalized types.
- **`CliAdapter`** (`runtimes/cli/base.py`): `detect`, `inspect_auth`, `capabilities`, `start_run`,
  `resume_run`, `cancel`. Each maps native output to `NormalizedEvent`s via a **pure** function
  (e.g. `map_codex_event`) that is unit-tested against recorded fixtures.
- **`NormalizedEvent`** (`core/events.py`): the shared vocabulary (`run.*`, `message.*`, `tool.*`,
  `command.*`, `file.*`, `usage.updated`, …) written to `events.jsonl`.

## Data model separation

- `ProviderConnection` — an API account (no prompt/role); key stored once.
- `ModelProfile` — a concrete model + probed capabilities.
- `AgentProfile` — what the user runs: runtime (API or CLI) + prompt + tags + permission profile.
- `Run` / `Session` — an execution and its resumable conversation.

## Run pipeline (`services/run_service.py`)

1. Allocate a run id and emit the run's **one and only** `run.started`.
2. **Preflight** (`services/preflight.py`): prove the agent can actually run — CLI present,
   executable, authenticated, adapter supports the requested mode (and for Codex: `codex exec` really
   accepts `--json` and the sandbox we are about to ask for); or, for an API agent: provider exists,
   credential ref valid, secret resolves, base URL resolves, model set, adapter constructs. A failed
   mandatory check blocks the run *before* a workspace exists.
3. Create an isolated git worktree (`openagent/run_<id>`), or a temp copy for non-git projects.
4. Dispatch to the API loop or a CLI adapter; stream `NormalizedEvent`s to `events.jsonl`.
5. Collect the diff, changed files, and test results.
6. Write the standard bundle (including `timeline.md` and the structured sections of `result.json`)
   and set the final status.
7. Support resume (CLI), cancel (real for both runtimes), and orphan recovery.

Phases are reported as they change: `preflight → preparing_workspace → starting_backend → running →
[waiting_approval | waiting_user] → finalizing → completed | failed | cancelled`. Each turn ends in
exactly one terminal event. A backend subprocess starting is a `process.started` (carrying the pid) —
a different fact from the *run* starting, which only OpenAgent may assert.

Every runtime exception becomes a persisted `run.failed` with a normalized `error_type`
(`cli_not_found`, `authentication_failed`, `provider_not_found`, `credential_missing`,
`workspace_failed`, `process_start_failed`, `schema_mismatch`, `provider_rate_limited`,
`insufficient_balance`, `context_limit`, `terminal_conflict`, `user_cancelled`, `unknown`), the phase
it failed in, and a redacted message — landing in `events.jsonl`, `status.json`, `result.json` and
`output.md`.

## Projection (`core/projection.py`)

`events.jsonl` is append-only: a plan being ticked off, a command finishing, a patch failing each
produce a *new* event. Readers therefore fold the log into current state, keyed by
`(source, turn, item_id)`.

The turn is part of the key because a backend may restart its item numbering each turn — Codex does,
so turn 2's first message is `item_0` again, and without the turn it would overwrite turn 1's card.

One projection feeds three consumers, so they cannot disagree: the live Run Console, `timeline.md`,
and the structured sections of `result.json`. Replaying the log rebuilds identical state, which is
what makes a run reopenable — live or finished — and what lets the console be closed without stopping
the agent (runs are owned by the Textual *app*, not by a screen, because a Textual worker dies with
the node that created it).

## Cancellation (`core/cancellation.py`)

One `RunCancellation` per run, shared by the canceller and the run loop. The authoritative flag is a
`threading.Event` — in the TUI the run executes on a worker thread's event loop while `cancel()` is
called from the UI loop, and `asyncio.Event` is not safe across loops; the awaitable mirror is set via
`call_soon_threadsafe`. The API loop checks it before each provider request, on every stream chunk,
around every tool call, and at the top of every step, so a cancelled API run stops instead of running
on to `completed`.

## Why httpx-native providers

Most providers are OpenAI- or Anthropic-compatible variants. A single transport per protocol family
maximizes reuse and gives full control over streaming/usage/error normalization. Vendor SDKs can be
swapped into individual adapters later without changing the `ProviderAdapter` contract.
