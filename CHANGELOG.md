# Changelog

All notable changes to OpenAgent are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and this project aims to follow
[Semantic Versioning](https://semver.org/).

## [0.1.6rc3] — unreleased

Third release candidate. Closes the release blockers left open in rc2 (see rc2's *Known gaps*), plus
the reader/writer compatibility work that turns the "stale binary reads a newer database" failure —
a raw Pydantic `ValidationError` from `ProviderConnection.model_validate` — into a clean, actionable
error. Every change ships with a regression test that fails against the unpatched code. Still a
release candidate: the RC soak, remote governance application, and a full Textual recovery screen /
interactive update-failure prompt (the underlying invariants hold; only the richer UI is deferred)
remain outstanding.

### Recovery & compatibility

- **A too-new database is refused from metadata alone, before any model loads.** An older binary
  opening a database a newer OpenAgent wrote died with a raw `ValidationError` deep in
  `ProviderConnection.model_validate` — a JSON-blob field (`credential_revision`, added in v0.1.4)
  the old domain model did not know. The integer schema number was identical in both builds, so the
  schema-version guard never fired. `schema_meta` now records `last_writer_version` and a monotonic
  `minimum_reader_version`, and `Database.open` gates on it from metadata alone, raising a typed
  `DatabaseReaderCompatibilityError` with the active binary, its version, the required version and
  the exact repair command. `version`/`update` never open the DB, so the repair path still runs on
  the old binary.
- **An undecodable provider row is quarantined, not fatal.** `ProviderRepository.list`/`get` decode
  through a boundary that raises a typed, redacted `DataValidationError` naming the record — never a
  raw traceback that also prints the payload (which can hold a credential ref, header or URL). A new
  `decode_report()` lets doctor survey the store and report every bad row without dying on the first.
- **The console entrypoint renders operational errors as clean messages.** `DatabaseReaderCompatibilityError`,
  `DataValidationError`, blocked migrations and schema-too-new all reach the user as a short,
  secret-redacted line with a stable exit code — or a structured `doctor --json` check — never a
  traceback. `OPENAGENT_DEBUG` re-raises for developers.

### Data integrity

- **Migration 0013 makes an API agent's provider binding a hard invariant.** 0012 gave `agents` a
  real `provider_id` foreign key (no *dangling* binding) but left a *missing* one as NULL — the
  "agent exists, provider missing" state. 0013 adds a named `CHECK (runtime_type != 'api-agent' OR
  provider_id IS NOT NULL)`; a legacy row already in the forbidden state blocks the upgrade, named
  and with the backup retained, never silently nulled or deleted. `_provider_id_for` fails closed
  with `ProviderNotFoundError` for an API agent whose provider is missing or unset.
- **Agent update/remove are compare-and-swap with a committed document projection.** They read the
  profile together with its revision and write through a CAS (`get_with_revision` + `delete_checked`),
  so a stale writer loses cleanly with `ConcurrentModificationError` instead of clobbering. The DB is
  the source of truth: a projection conflict is never rolled back, the journal entry stays pending
  for retry, and doctor reports it.
- **OPENAGENT.md samples its snapshot inside the document lock.** The committed agent set is read
  after the cross-process lock is held, so a snapshot taken before the lock cannot be regenerated
  over a document another process wrote from a newer commit.
- **Model/CLI upserts use native `INSERT … ON CONFLICT`,** not DELETE-then-INSERT, so the row is
  never transiently absent and a failure cannot leave it deleted.

### Security

- **Git content filters are neutralized.** A `.gitattributes` binds paths to a named filter whose
  clean/smudge/process commands git runs on checkout and `git add`. Each filter that could execute is
  discovered per call and overridden to an identity (`clean=cat`, `smudge=cat`, `process=`,
  `required=false`) on every invocation.
- **Claude auth no longer trusts a config file, and OAuth tokens are never sent as `x-api-key`.**
  `~/.claude.json`/`settings.json` are configuration, not credentials, so their mere presence now
  reports UNKNOWN (never `true`); only `~/.claude/.credentials.json` counts as a login. Direct
  Anthropic `/v1/models` discovery uses `x-api-key` with `ANTHROPIC_API_KEY` only — an OAuth/session
  token is never sent there; a gateway authenticates through a dedicated `GatewayAuthPlan`.

### Reliability

- **Codex discovery has an expiring capability cache and a continuous stderr drain.** A transient
  probe failure no longer disables `model/list` for the whole process (short TTL, `refresh` bypass),
  and the app-server's stderr is drained to EOF with a bounded, redacted tail so a chatty server
  cannot fill the pipe and deadlock the handshake; past a hard limit the runaway process is
  terminated.
- **Update-prompt suppressions are cross-process safe and expire by age.** The read-modify-write runs
  under a file lock and re-reads inside it (no lost entry under concurrent dismissals), records carry
  `{fingerprint, created_at, expires_at}`, and eviction is by age rather than hash-alphabetical order.

### Known gaps

- The RC soak (real-use burn-in) and remote governance application (branch ruleset, labels,
  milestones via `gh`) are still outstanding, and gate the final `0.1.6`.
- A full Textual recovery screen and the interactive "update failed → continue / cancel / doctor"
  prompt are deferred; the underlying invariants (compat error rendered cleanly at startup, an
  unverifiable self-update treated as not-ok, a below-minimum version blocking a run) already hold.

## [0.1.6rc2] — unreleased

Second release candidate. Closes release blockers found by independent audit and real GitHub Actions
runs against `0.1.6rc1`. Every change ships with a regression test that fails against the unpatched
code; the reproduction is recorded in each commit message. This is a release candidate, not a final
release — the RC soak, remote governance application, and the remaining service-concurrency items
(see *Known gaps* below) are still outstanding.

### Updater

- **A stale binary is repaired even when the source checkout is already current.** `openagent
  update` decided a source-checkout install was up to date purely from
  `local_revision != remote_revision`. A non-editable install is a *copy*: the checkout can be level
  with `origin/main` while the binary on `PATH` is an old snapshot, so the installer never ran and
  `openagent version` kept reporting the old number. The decision now rests on two independent axes —
  a revision update *and* installation drift (active binary version vs. the checkout's declared
  version) — and reinstalls on either. An unreadable active version is treated as drift, not silently
  current; a checkout that cannot state its own version fails closed. Adds `openagent update
  --repair`/`--force-reinstall` to force a reinstall from the proven source when versions already
  match.
- **Version comparison is unified behind one authority.** The self-updater still re-derived version
  parsing with a regex and an integer tuple, so `0.1.6rc1` parsed to `0.1.6` and a release candidate
  compared equal to its release — the same class of bug already fixed in the CLI updater. All version
  decisions (self-update, CLI minimum-version policy, `version_verified`, update verification) now
  route through `openagent.core.versioning`, built on PEP 440 (`packaging`). Anything unparseable is
  reported as unknown and never silently treated as equal, newer, or at-least. A CLI minimum-version
  policy that cannot be evaluated now fails closed instead of silently passing.

### Fixed

- **A fast backend process is no longer mistaken for a failed launch.** `ManagedProcess.start()`
  sampled the child's identity synchronously, yielded the loop once, and — if the return code had not
  been published yet — terminated the (already finished) child and raised, so a valid, fast command
  that exited during the startup window was reported as a startup failure. Startup now runs a bounded
  async handshake that races the child's exit against identity sampling (off the event loop, so it no
  longer stalls other runs): a child that completes first is a success with no identity to capture; a
  child still alive at the deadline with no readable identity is terminated fail-closed with a typed
  `ProcessIdentityCaptureError`; a cancel arriving mid-capture waits for the capture to settle rather
  than refusing to kill a process it owns.

### Data integrity

- **Provider and agent creation is insert-only and database-authoritative.** Both creations wrote
  through `upsert`, which silently overwrote a colliding row, so two processes that each passed the
  service-level `get(name) is None` pre-check could both "create" the same name and the second
  clobbered the first. Both now go through the repositories' insert-only `create`, whose uniqueness
  (including the case- and Unicode-folded form) is decided by the database. A duplicate surfaces as a
  typed validation error, and the provider transaction's compensation removes only its own
  revision-scoped secret, never a pre-existing row or another transaction's secret.

### Known gaps

- Compare-and-swap on agent *update*/*remove*, the committed-agent document-projection semantics
  (DB as source of truth on a projection conflict), the OPENAGENT.md snapshot-under-lock change, the
  provider-ownership / API-binding migration `0013`, the Claude auth-fallback and OAuth-header
  hardening, the Git content-filter neutralization, and the Codex discovery cache/stderr work are
  **not** in this candidate. They remain tracked release work.

## [0.1.6rc1] — unreleased

Release candidate covering the v0.1.5 (authentication, Git, updater) and v0.1.6 (provider/agent
concurrency, generated-file locking) stabilization work. Every change ships with a regression test
that fails against the unpatched code; the reproduction is recorded in each commit message.

### Security

- **Internal Git subprocesses are isolated.** Committing an agent's work no longer hands the parent
  process's environment — every provider API key included — to a `.git/hooks/pre-commit` chosen by
  the repository being worked on. All internal `git` calls route through `security/git_runner`,
  which builds a minimal environment (allowlist, not denylist), disables hooks via an empty
  `core.hooksPath` (not `--no-verify` alone), clears every configuration-named delegation point
  (pager, credential helper, external diff, textconv, ssh command, `ext::` protocol), and
  terminates the whole process tree on timeout. The user's own `git` is untouched.
- **CLI credentials reach only the CLI they belong to.** A Claude run receives Anthropic variables,
  a Codex run receives OpenAI variables, and neither receives the other's key or any unrelated
  cloud credential. Values are registered for output redaction before the child starts and released
  when the turn ends; they are resolved per turn and never persisted, so a rotated key takes effect
  on resume.

### Fixed

- **Authentication detection was wrong in both directions.** An exported `ANTHROPIC_API_KEY` /
  `CLAUDE_CODE_OAUTH_TOKEN` — the documented way to authenticate — no longer reports "not signed in"
  and blocks the run, and a `~/.claude.json` config file is no longer mistaken for a credential.
  Detection now asks `claude auth status` (JSON) under the environment the run will use; Codex
  evidence is gathered from both `codex login status` and the environment because neither is
  sufficient alone. "Could not determine" no longer blocks — only a known absence does.
- **A failed update reported success.** `openagent cli update` no longer exits 0 when the binary is
  unchanged or its version cannot be compared; both are `VERIFICATION_FAILED`. Version comparison
  uses PEP 440 (`packaging`) instead of a regex that treated `1.2.0rc1` and `1.2.0` as equal. NPM
  provenance fails closed when `npm prefix`/`root` cannot be read, rather than assuming ownership.
  Concurrent updates of one CLI are excluded by a cross-process lock.
- **`CliUpdatePolicy.ASK` now asks.** It previously behaved identically to `NOTIFY` — the default
  policy never prompted. A non-interactive session degrades to `NOTIFY` rather than hanging; "don't
  ask again" is scoped to the exact version and binary so it never silences a later update.
- **Claude model discovery is project- and credential-aware.** `list_models` was called with no
  context, discarding a project's `availableModels` policy and never performing the `/v1/models`
  lookup. Doctor now runs real discovery instead of reporting OK because a method name exists.
- **Provider and agent records no longer lose data under concurrency.** Agent names could be
  silently overwritten (DELETE+INSERT), provider names collided on case and Unicode form, stale
  writes clobbered newer ones, and an agent could outlive its provider. Uniqueness (case- and
  Unicode-folded), the agent→provider foreign key, and `state_revision` compare-and-swap now live in
  the database. Concurrent edits raise `ConcurrentModificationError` rather than winning silently.
- **OPENAGENT.md no longer destroys hand-written prose.** A malformed marker block is refused with
  an actionable `OpenAgentMdConflict` (and the new `openagent agent sync-document --dry-run`) rather
  than replacing the whole file with boilerplate, and concurrent regeneration is serialized under a
  cross-process lock with a preimage check. A conflicted document never blocks startup.

### Added

- **Revision `0012`** — `normalized_name`, `state_revision`, and `updated_at` on providers and
  agents; a real `agents.provider_id` foreign key with `ON DELETE RESTRICT`, backfilled from the
  provider name. Pre-existing duplicates block the migration (naming the rows) rather than being
  resolved by guesswork; the pre-migration backup is retained.
- **WAL journal mode with `synchronous=FULL`** on every connection.
- **`openagent agent sync-document`** — regenerate OPENAGENT.md, with `--dry-run` to preview.
- **Doctor checks** for model discovery, the operation journal, and provider/agent integrity.
- **Quality infrastructure** — CodeQL (`security-extended`), Dependabot, branch-coverage measurement
  with a per-module ratchet, issue/PR templates, and a v0.1.4 baseline report.

## [0.1.4] — 2026-07-18

Lifecycle/concurrency hardening, source-aware CLI and OpenAgent updates, truthful model discovery,
cross-process monitoring, atomic domain migrations, and a completed container/installer audit.

### Added

- **`openagent update`** — DB-independent `--check`, `--dry-run`, interactive, `--yes`, and `--json`
  flows. Clean official source checkouts fast-forward `main` and re-run the platform installer;
  index installs use their owning uv-tool/pipx/exact Python environment. PATH, revision, expected
  version, and Doctor health are verified after mutation.
- **Coding CLI lifecycle framework** — cross-platform candidate enumeration, safe realpath/provenance
  inspection, active-vs-shadowed installation reporting, cached official update metadata,
  source-matched non-elevated updaters, active-run/conflict blocking, audit events, and post-update
  rediscovery for Codex, Claude Code, and Antigravity.
- **Source-specific model discovery** — Codex app-server `model/list`; layered Claude
  API-key/config/alias discovery without scraping an interactive picker or claiming subscription
  entitlement; and account-context `agy models`. Structured results distinguish a valid empty list,
  partial catalog, auth/rate-limit/timeout/network/malformed response, and unsupported endpoint.
- **Cross-process Run Console tailing** — one SQLite replay followed by bounded `seq > cursor`
  queries, local/polled duplicate prevention, reopen handling, and terminal stop semantics.
- **Revisions `0008`–`0011`** — real run foreign keys and turn leases, revision-consistent run JSON,
  exact legacy NVIDIA Build normalization, and streaming Pydantic validation of all domain tables.

### Changed

- Run lifecycle writes are revision-aware compare-and-set operations. Relational status/phase/lease
  fields and JSON payloads mutate together; live process leases cannot be stolen and dead owners are
  recovered without leaving a permanent `running` state.
- Event JSONL is explicitly an export/recovery surface: first event, batch boundary, terminal event,
  explicit flush and shutdown are durable refresh points. SQLite is authoritative and no fixed
  250 ms JSONL freshness claim is made.
- Provider catalogs return structured discovery outcomes. Dashboard and Doctor isolate corrupt
  provider/agent/CLI/event sources so one incompatible record does not remove the diagnostic route.
- Tool execution converts documented operational exceptions into bounded redacted failures, while
  unexpected invariant errors become a generic internal failure and cancellation/system exits are
  never swallowed.
- The container backend runs as UID/GID `65532`, keeps default seccomp, explicitly uses private
  PID/IPC namespaces, never pulls/builds/falls back, performs all-file sync conflict preflight,
  preserves executable bits, and cleans up on timeout/cancel.
- Installers verify the exact source version and PATH winner, parse Doctor's exit-code contract,
  display migration backups, and refuse TUI launch on database/migration/event integrity failures.
  CI covers repeat/path-with-spaces/old-shadow installs, v0.1.2 and v0.1.3 wheel upgrades, future
  schema, corrupt JSON, migration rollback, all supported OS/Python versions, and real Docker.

### Fixed

- Artifact-finalization failures can no longer emit or preserve a false completed terminal state;
  every recovery path rebuilds a consistent failure bundle.
- Event append handles short OS writes and Doctor accepts only the valid ordered terminal chains,
  including `orphaned → cancelled`.
- Legacy NVIDIA records retain provider/model/agent identities and credential references; unrelated
  custom OpenAI endpoints are not reclassified.
- TUI markup keeps `[REDACTED]` visible and inert, while all required screens/modals retain their
  final action and focus/scroll behavior from 120×40 through 40×12.

### Known limitations

- Live paid-provider inference and live Codex/Claude/Agy audits remain opt-in and environment
  dependent. Claude subscription/OAuth has no public scriptable entitlement catalog.
- `container-sandbox` supports structured API-agent tools; long-lived CLI adapters remain refused.

## [0.1.3] — 2026-07-16

Security, data-integrity, project-scoping, responsive-TUI, and release hardening.

### Added

- **Execution backends** — default `host-restricted` policy execution plus an explicit
  `container-sandbox` for structured API-agent tool commands. The container uses an already-local
  Linux image, no host mount/network, read-only root, tmpfs workspace/`/tmp`, dropped capabilities,
  no-new-privileges and CPU/memory/PID quotas. Missing images/runtimes and unsupported CLI adapters
  fail closed without pull/build/host fallback.
- **Stable projects** — `.openagent/project.json`, SQLite `projects`, active-project defaults,
  explicit `--all-projects`, and `project list/relocate` for moved or missing roots.
- **SQLite-authoritative events** — complete event bodies and transactional sequence allocation,
  atomic JSONL export, Doctor consistency diagnostics, and `events repair`.
- **Durable operation journal** for provider/keychain/agent/`OPENAGENT.md` mutations, with startup
  compensation/completion and revision-scoped provider credential references.
- **Model verification metadata** — persisted probe version/expiry/capability snapshot/fingerprints,
  explicit override status and mandatory override reason. Catalog entries remain tri-state unknown.
- **Artifact/Git provenance** — SHA-256 `integrity.json`, optional clean OpenAgent-worktree commits,
  agent/model attribution, `rerun` with a new run ID, and `revert` via a new revert commit.
- **Responsive TUI contract** — seven terminal sizes down to 40×12, fixed action bars, scrollable
  modals, focus/page/home/end/mouse/resize behavior, explicit follow-output state, bounded LiveRun
  retention and deterministic SVG snapshots.
- **PowerShell installer** (`setup.ps1`) and CI jobs for a real Docker sandbox, v0.1.2 wheel/DB
  upgrade plus backup restore, Windows PowerShell install, and fresh current-wheel install.

### Changed

- Migrations are an immutable `0001`–`0007` revision chain. Upgrades use `BEGIN IMMEDIATE`, SQLite
  online backup, integrity/FK checks and critical row-count verification; unknown revisions and
  interrupted upgrades fail closed.
- All filesystem/copy/baseline/diff/artifact access uses a budgeted no-follow walker; atomic writes
  use temp/write/flush/fsync/chmod/replace/directory-fsync.
- API transport has fixed connect/read/write/pool/total timeouts, bounded Retry-After/retries,
  cancellation-aware sleep, no retry after the first stream event, strict malformed/tool-call
  failures and central byte/count limits.
- All machine-readable CLI output goes directly through one JSON emitter; human warnings use stderr.

### Fixed

- Cross-process cancellation now verifies PID, creation time, executable and command identity,
  terminates/kills survivors, then verifies again. Only `terminated` changes persisted run state.
- General interpreters/shells/Git/file utilities no longer receive automatic test authority.
  `run_tests` accepts only exact structured pytest/npm/pnpm/yarn/cargo/go/dotnet test argv shapes.
- Git diff/status uses NUL-delimited porcelain without touching the user's index; cleanup requires
  OpenAgent ownership metadata and in-place user changes are never committed.
- Secret registration is run-scoped, thread-safe and reference-counted. Display sanitization and
  every TUI password-widget exit/worker path now clear or redact secrets deterministically.
- CLI stdout, final messages, provider errors, events, tool arguments, model/history text, diffs and
  projections are bounded with visible truncation or `output_limit_exceeded` outcomes.

### Known limitations

- `container-sandbox` currently executes API-agent tool commands. Long-lived CLI adapters are
  refused under this backend rather than silently running on the host.
- Live provider/CLI audits remain environment/credential dependent and are reported separately from
  the offline/real-container CI gates.

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
[0.1.2]: https://github.com/yasirkaramandev/openagent/releases/tag/v0.1.2
[0.1.3]: https://github.com/yasirkaramandev/openagent/releases/tag/v0.1.3
[0.1.4]: https://github.com/yasirkaramandev/openagent/releases/tag/v0.1.4
