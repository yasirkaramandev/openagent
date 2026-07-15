# Changelog

All notable changes to OpenAgent are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and this project aims to follow
[Semantic Versioning](https://semver.org/).

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
