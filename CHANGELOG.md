# Changelog

All notable changes to OpenAgent are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and this project aims to follow
[Semantic Versioning](https://semver.org/).

## [0.1.2] — 2026-07-16

Orphan/resume hardening and NVIDIA Build integration.

### Added
- **NVIDIA Build provider** (`nvidia-build`) — the hosted NIM API catalog at
  `https://integrate.api.nvidia.com/v1` over OpenAI Chat Completions, with the key taken from a
  hidden prompt or an `NVIDIA_API_KEY` env-var reference (never `argv`). Hosted NVIDIA Build is kept
  distinct from self-hosted NIM, which continues to use the `custom` preset.
- **NVIDIA catalog discovery** — `openagent provider models` gains `--search`, `--owner` and `--json`;
  `owned_by` is preserved so a mixed catalog can be filtered by publisher, locally and offline.
  A catalog entry is never presented as agent-compatible (`capabilities` is always `null`).
- **NVIDIA model capability probing** — a new `openagent provider probe` really exercises a model
  (text, streaming, tool calling) with bounded requests and a strict timeout, claims only what it
  observed, and classifies failures honestly (unauthorized / not found / incompatible / async /
  rate limited). Results are cached per connection+model+base-URL+credential identity with a 24h TTL;
  rotating the key invalidates a prior "verified". `openagent add` refuses an unvalidated
  mixed-catalog model unless `--allow-unverified-model` is passed, which is loudly reported.
- **NVIDIA TUI and CLI setup** — an NVIDIA Build card, a provider-aware Connection step (fixed
  official endpoint/protocol, keychain recommended, `NVIDIA_API_KEY` pre-filled, no "no key" option,
  key instructions, and an "Open NVIDIA Build" button using `webbrowser.open`), plus a searchable,
  publisher-filtered catalog browser with a mixed-catalog warning and a "Validate Model & Key" probe.
- **NVIDIA API key redaction** — an `nvapi-` pattern alongside the exact `register_secret()`
  mechanism, so keys never reach CLI/TUI output, `events.jsonl`, `result.json`, `timeline.md`,
  `logs.txt`, `changes.diff`, or exception text.

### Fixed
- **Orphaned live-process cancellation** — `cancel()` used to reject every terminal status, so an
  `orphaned` run whose process was *still alive* could not be stopped — even though orphan recovery
  told the user to run `openagent cancel --id <run-id>`. It now handles orphans before the terminal
  short-circuit, re-verifies PID + create-time identity, and terminates the process tree; a
  gone/reused/unverifiable PID is never killed. `cancel()` returns an explicit outcome
  (`signalled` / `terminated` / `already_terminal` / `not_found` / `identity_mismatch` /
  `not_cancellable`) and the CLI reports it with the right exit code instead of always printing
  "cancelled".
- **Resume lifecycle hardening** — a follow-up turn now obeys the same contract as the first run:
  the turn's terminal event is buffered and written **last** (exactly one, keeping the backend's
  richer data), and adapter build, backend stream, diff, every artifact write and DB persistence all
  live inside one exception boundary, so no failure can leave a run "running" or report success over
  a failed artifact write.
- **Concurrent follow-up locking** — a per-run lock rejects a second follow-up with "a turn is
  already running for this run" instead of silently overwriting the first turn's adapter and
  cancellation registry.
- **Artifact recovery consistency** — failure recovery now rewrites the *whole* bundle
  (`status.json`, `result.json`, `timeline.md`, `output.md`, `handoff.md`, `tests.json`,
  `changes.diff`, `logs.txt`) and marks it `artifacts_partial` with the failing stage. A terminal-event
  append failure can no longer leave a stale "completed" `timeline.md` behind.
- **Windows persisted PATH verification** — `setup.bat` now checks the PowerShell exit code when
  writing the user PATH (a failed write fails the install), re-reads the registry to prove the tool
  directory was persisted, and verifies a *fresh* CMD **and** PowerShell can run `openagent` using the
  persisted PATH — instead of a test that injected the tool directory by hand and passed even when the
  registry update had failed entirely.
- **Transaction-local credential rollback** — the service-level rollback cache that kept the previous
  keychain secret in plaintext for the process lifetime is gone. Rollback state now lives only on the
  transaction stack and is wiped on commit, so a *successful* provider add retains nothing.
- **Raw reasoning is never stored** — `reasoning_content` (NVIDIA and others) is treated as raw
  chain-of-thought and never reaches an event, artifact, or the UI; only the numeric reasoning-token
  count is normalized. An HTTP 202 (async invocation) is now an explicit failure rather than being
  read as an empty success.

### Changed
- **Repository-wide formatter baseline** — `ruff format` is applied across the repo, the ruff version
  is pinned, and `ruff format --check .` is now an enforced CI gate.
- **Honest provider testing** — `openagent provider test` reports "catalog reachable" and states that
  the key and model inference are *not* yet validated; it never claims "authenticated" or "API key
  valid". Reaching `/models` proves neither.
- **Skill accuracy** — the AI skill no longer says "orphaned = the process is gone". It explains that
  `orphaned` means OpenAgent lost ownership and that the process may be gone, reused, unverifiable, or
  still alive, teaches identity-verified cancellation, and documents the NVIDIA Build flow.

## [0.1.1] — 2026-07-15

Runtime hardening, one-command cross-platform install, and an AI skill.

### Added
- **Cross-platform bootstrap installers** — `setup.sh` (macOS/Linux) and `setup.bat` (Windows), built
  on [uv](https://docs.astral.sh/uv/). No pre-installed Python needed.
- **Managed Python through uv** — the installer downloads an isolated Python 3.12; the system Python
  is never touched and no `.venv` is created in the repo.
- **Terminal-wide `openagent` command** — the installers link `openagent` onto PATH so a new terminal
  runs it directly; `OPENAGENT_SETUP_NO_LAUNCH=1` verifies without opening the TUI (for CI).
- **AI skill documentation** — `skills/openagent/SKILL.md` (+ `skills/README.md`) teaches assistants
  the safe CLI workflow: setup, model selection, running, artifacts, cancellation, resume, security.
- **Installer CI** — Ubuntu/macOS/Windows jobs that run the installers without `actions/setup-python`.

### Fixed
- **Runtime cancellation hardening** — a stalled provider stream (no new chunk) is now cancellable
  (the read is guarded by the cancellation event), and a blocking `run_command`/`run_tests` is
  cancelled mid-flight, terminating the whole child + grandchild process tree.
- **Bounded API tool output** — `run_command`/`run_tests` enforce a real byte limit as the process
  runs (`OutputLimitExceeded` → a safe `ToolError`), instead of only truncating afterward.
- **Artifact lifecycle hardening** — the whole run is inside one exception boundary with atomic
  (temp-file + replace) artifact writes; a setup/finalize failure can never leave a run "running" or
  make an artifact-write failure look like success.
- **Orphan recovery correction** — a live but unowned run (a restart can't reattach its stream) is
  marked `orphaned_unattached_process` (recorded, not killed), not left "running".
- **CLI model persistence** — `openagent add --cli … --model …` now persists the model and it reaches
  the run argv (previously silently dropped on the CLI path); `--model` help text corrected.
- **Separate model-selection wizard step** — model choice is its own page in the Add-Agent wizard,
  with per-backend discovery, manual/default/verified status, and no leakage across backend changes.
- **CLI markup escaping** — the CLI event renderer and tables escape model/command/path/error values.
- **Repository rename URL cleanup** — all `open-agent` URLs updated to `openagent`.

[0.1.1]: https://github.com/yasirkaramandev/openagent/releases/tag/v0.1.1

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
