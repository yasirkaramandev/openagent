# Architecture

A short map of the codebase. The guiding idea: **one normalized event stream and one artifact bundle**
regardless of which backend runs the work.

## Layers

```
Interfaces      tui/ (Textual)   ·   cli/ (Typer)        [MCP, SDK planned]
                        │
Services        services/  — the single business layer (agent, provider, model,
                             run, discovery, self-update, doctor). CLI + TUI both call these.
                        │
Runtimes        runtimes/api_agent/  — OpenAgent's own loop for API models
                runtimes/cli/        — subprocess adapters (codex, claude, generic)
                        │
Substrate       providers/  tools/  workspaces/  security/  credentials/
                        │
Storage         storage/ (SQLite authoritative events + revisions) · JSONL export · reporting/
```

## Key contracts

- **`ProviderAdapter`** (`providers/base.py`): `test_connection`, `list_models`, `probe_model`,
  `stream_response`, `count_tokens`. The agent loop speaks only normalized types.
- **`CliAdapter`** (`runtimes/cli/base.py`): candidate location/provenance inspection, update
  checking/execution, model discovery, `inspect_auth`, `capabilities`, `start_run`, `resume_run`, and
  `cancel`. The locator records the real PATH winner and every independent shadow before an updater
  is selected. Cancellation returns a structured identity-verified termination result. Each adapter
  maps native output to `NormalizedEvent`s via a **pure** function
  (e.g. `map_codex_event`) that is unit-tested against recorded fixtures.
- **`ExecutionBackend`** (`security/execution_backend.py`): `host-restricted` policy execution or an
  explicitly selected local-image Docker/Podman tmpfs sandbox. Unsupported combinations fail closed.
- **`NormalizedEvent`** (`core/events.py`): the shared vocabulary (`run.*`, `message.*`, `tool.*`,
  `command.*`, `file.*`, `usage.updated`, …) stored in SQLite and atomically exported to JSONL.

## Data model separation

- `ProviderConnection` — an API account (no prompt/role); key stored once.
- `ModelProfile` — a concrete model + probed capabilities.
- `AgentProfile` — what the user runs: runtime (API or CLI) + prompt + tags + permission profile.
- `Run` / `Session` — an execution and its resumable conversation.

## Run pipeline (`services/run_service.py`)

1. Resolve the active stable project UUID, allocate a run id, and emit the run's **one and only**
   `run.started`.
2. **Preflight** (`services/preflight.py`): prove the agent can actually run — CLI present,
   executable, authenticated, adapter supports the requested mode (and for Codex: `codex exec` really
   accepts `--json` and the sandbox we are about to ask for); or, for an API agent: provider exists,
   credential ref valid, secret resolves, base URL resolves, model set, adapter constructs. A failed
   mandatory check blocks the run *before* a workspace exists.
3. Create an isolated git worktree (`openagent/run_<id>`), or a temp copy for non-git projects.
4. Claim a revision-aware turn lease tied to process create-time identity, then dispatch to the API
   loop or a CLI adapter. Store each complete `NormalizedEvent` and allocate its sequence in one
   SQLite write transaction. JSONL is refreshed at explicit batch/terminal/flush/shutdown boundaries.
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

Lifecycle mutation is optimistic and revision-aware. Relational status/phase/lease columns and the
JSON domain payload are changed together in one compare-and-set transaction; a stale process cannot
overwrite a newer terminal state. A dead lease is recovered to one deterministic terminal chain,
while a live lease cannot be stolen merely because wall-clock time elapsed.

## Projection (`core/projection.py`)

The SQLite event stream is append-only: a plan being ticked off, a command finishing, a patch failing
each produce a *new* event. Readers therefore fold the stream into current state, keyed by
`(source, turn, item_id)`.

The turn is part of the key because a backend may restart its item numbering each turn — Codex does,
so turn 2's first message is `item_0` again, and without the turn it would overwrite turn 1's card.

One projection feeds three consumers, so they cannot disagree: the live Run Console, `timeline.md`,
and the structured sections of `result.json`. Replaying the log rebuilds identical state, which is
what makes a run reopenable — live or finished — and what lets the console be closed without stopping
the agent (runs are owned by the Textual *app*, not by a screen, because a Textual worker dies with
the node that created it).

The Run Console reads SQLite, not JSONL: it performs one ordered replay, remembers the highest
sequence, and polls only newer rows in bounded batches. Local events are marked against the same
cursor, so a local publish racing a database poll applies once. This makes a writer in another
OpenAgent process visible without O(n²) full-history rereads; database replacement/reopen and a
terminal event are explicit tailer states.

## CLI and OpenAgent update lifecycle

`runtimes/cli/locator.py`, `installations.py`, `updates.py`, and `model_discovery.py` keep four facts
separate: executable discovery, installation provenance, official update metadata, and catalog
discovery. Network checks are explicit/cached. An updater is unavailable until provenance is proven,
and is blocked by shadowed copies or active runs. Post-update discovery verifies the exact path,
source, and version again.

`services/self_update.py` intentionally does not open `OpenAgentApp` or the database. It can repair
the application even when DB startup fails, then verifies PATH/version and finally runs Doctor. A
local source install fast-forwards only a clean official `main`; uv-tool/pipx/index installs update
through their owning environment.

## Storage revisions and backups

The immutable migration chain is atomic across every pending revision. Before mutation SQLite's
online backup API creates the reported backup. Verification covers integrity, foreign keys,
row-counts and identities, schema parity, mirrored relational/domain fields, event sequences, and
batch Pydantic validation of provider/model/agent/CLI/project/run/session/event/probe/usage JSON.
Pending failure rolls the chain back and exits `3`; an already-current incompatible database exits
`2`. Revision `0010` narrowly normalizes only exact legacy NVIDIA Build endpoints and `0011` pins the
domain-validation invariant.

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
