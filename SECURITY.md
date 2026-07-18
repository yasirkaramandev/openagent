# Security

OpenAgent runs AI backends that can read, edit, and execute code on your machine. These are the
guardrails it enforces, and how to report issues.

## Threat model & scope (read this first)

The default `host-restricted` backend enforces guardrails at the **policy / approval layer** and
through workspace isolation; it is not an OS sandbox. An explicitly selected `container-sandbox`
adds a Docker/Podman boundary for structured API-agent tool commands. Concretely:

- Under `host-restricted` there is **no OS-level network namespace or firewall**. A "no-network" profile does not block
  sockets at the kernel level — it routes network-*oriented* commands (`curl`, `pip install`,
  `git clone`, …) through **approval**. Once a command is approved, or if an agent runs an
  allowlisted binary that happens to open a socket, nothing at the OS level prevents that.
- Under `host-restricted` there is **no OS-level filesystem sandbox** around child commands.
  OpenAgent's *own* filesystem tools (`read_file`, `write_file`, `apply_patch`, …) validate every
  path to stay inside the workspace and reject traversal/symlink escapes — but that validation does
  **not** extend to subprocess commands. A shell command runs with its working directory set to the
  workspace; it can still read or write outside it via absolute paths or `cd`.
- Isolation of *your* project therefore comes from running the agent in a **git worktree or a
  directory copy** (see Workspace isolation), not from confining the subprocess.
- For **CLI agents** (Codex, Claude Code), OpenAgent maps each profile onto that CLI's own
  sandbox/permission flags (e.g. Codex `--sandbox`). Any real OS sandboxing there is provided and
  enforced by the CLI, not by OpenAgent.

`container-sandbox` is opt-in and fail-closed: Docker is preferred then Podman, the user must name an
already-local Linux image containing `/bin/sh`, and OpenAgent never pulls, builds or falls back to
the host. It rejects in-place workspaces, copies only no-follow regular files into a quota-limited
`/workspace` tmpfs, mounts no host path, disables network, uses a read-only root filesystem, drops
all capabilities, enables no-new-privileges, runs as UID/GID `65532:65532`, retains the runtime's
default seccomp filter, uses private PID and IPC namespaces, and limits the container to 2 CPU,
2 GiB memory/swap, 256 PIDs, 1 GiB workspace and 256 MiB `/tmp`. Sync-back performs a complete
conflict preflight before changing any host file, accepts only regular files, and preserves the
executable bit; a concurrent host edit aborts the whole sync. Timeout and cancellation remove the
container. Long-lived streaming CLI adapters are refused under this backend; use it for API-agent
tool execution or run OpenAgent itself in a separately managed container/VM.

## Credentials

- API keys are stored in the **OS keychain** by default (via `keyring`). Alternatives: reference an
  environment variable (`--key-env`), a session-only secret, or an external command.
- Keys are **never** written to the SQLite DB, `events.jsonl`, `logs.txt`, `OPENAGENT.md`, or passed
  as command-line arguments.
- For CLI subprocesses, a run's credential is injected **only into the child process environment**;
  the parent process environment is not used to carry provider keys.
- Provider/keychain/agent/`OPENAGENT.md` changes use a secret-free compensating-operation journal.
  Startup completes or compensates interrupted operations; a provider secret is never irreversibly
  deleted before its database transaction is durable.

## Secret redaction

Every string written to a run artifact passes through a redactor before it hits disk — this includes
`request.json` (**the user prompt is redacted**), `result.json`, `status.json`, `events.jsonl`,
`logs.txt`, `output.md`, `handoff.md`, and **`changes.diff`** (a diff can easily contain a pasted
secret). The redactor masks common secret shapes (`sk-…`, `Bearer …`, `Authorization: …`,
`*_API_KEY=…`, GitHub tokens) and also scrubs **exact key values registered at runtime** — required
for provider keys whose format has no recognizable prefix. Exact secrets are scoped to a run/call,
thread-safe and reference-counted. Display text is processed in the fixed order control removal →
redaction → byte limit → single-line normalization → markup escaping. Password widgets are wiped on
success, failure, cancellation, source/provider change, worker termination, unmount and shutdown.
Run artifacts are written with owner-only permissions where the platform supports it.

## Workspace isolation

Three explicit strategies (`--worktree`):

- **`auto`** — a git repo runs in an **isolated git worktree** on a fresh `openagent/run_<id>` branch;
  your working tree is untouched until you apply/merge. A non-git project falls back to an isolated
  **copy**, flagged **lower safety**.
- **`copy`** — always an isolated directory copy. Changed/created/deleted files and a unified diff are
  computed by comparing the copy to the untouched source.
- **`none`** — runs directly in your project directory; file-editing agents require **explicit
  confirmation** (`-y`) because there is no isolation.

OpenAgent's own filesystem/copy/diff/artifact paths go through one no-follow walker with file/type,
count, byte, deadline and cancellation budgets; symlinks, junctions, FIFOs, sockets and devices are
not traversed. **This does not confine host subprocesses** — an approved command runs with its cwd
set to the workspace but is not otherwise confined, so isolation of your real project comes from
the worktree/copy, not from blocking the command.

## Command policy

Commands run with a **minimal environment** (never the parent process's environment, so provider
keys, `GITHUB_TOKEN`, AWS keys, `DATABASE_URL`, etc. cannot leak into a child) and with `shell=False`
and a structured argument list. The **primary boundary is an executable allowlist**, not a regex
denylist:

- **Allowed automatically** — only narrow, structured operations. `run_tests` accepts exact argv
  shapes for pytest, `python -m pytest`, npm/pnpm/yarn, cargo, go and dotnet tests. A chain,
  redirection or second executable loses test authority. General interpreters/shells/Git/file
  utilities are never automatically trusted merely because of their executable name.
- **Requires approval** — any executable *not* on the allowlist, shell interpreters (`sh`, `bash`…),
  shell-operator commands (pipes/redirects/subshells), destructive verbs (`rm -rf`, `git reset
  --hard`, `git clean`, disk-level ops), and **network-oriented commands** under a no-network
  profile (`curl`/`wget`, `pip install`, `npm install`, `git clone/fetch/pull`, …). This is a
  **policy/approval gate**, not a kernel-level network block: it catches known network-*invoking*
  commands, and an approved command — or an allowlisted binary that opens its own socket — can still
  reach the network.
- **Denied** categorically — `git push`, `npm publish`, `pip/twine upload`, `docker login`, cloud CLI
  logins, `sudo`, reads of `.env` / SSH keys / credentials, and direct keychain access.

Approvals are recorded as `approval.requested` / `approval.accepted` / `approval.denied` events. A
non-interactive run **denies** high-risk operations by default and never silently auto-approves.

## Process management

CLI subprocesses run with a minimal environment. A process identity contains PID, creation time,
resolved executable and command identity. Cancelling terminates the **entire process tree**
(graceful terminate → survivor kill → second verification); only a verified `terminated` result may
change cross-process run state. Identity mismatch/unknown, access denied, termination failure and
remaining survivors are explicit outcomes. Cancelled/orphaned runs are terminal; rerun creates a new
ID. Startup orphan recovery is project-scoped and uses compare-and-set so racing processes produce
one transition/terminal event.

Coding CLI updates and OpenAgent self-updates use the same minimal-environment principle: provider
credentials are removed while proxy/CA variables needed by the package manager are retained.
Structured argv, output/time bounds, exact executable verification, provenance checks, shadowed-copy
blocking, active-run blocking and non-elevated execution prevent an updater from silently mutating a
different installation. A source-checkout self-update accepts only a clean `main` checkout with the
official Git origin and uses `pull --ff-only`.

## Storage, Git, and output integrity

- SQLite stores complete event JSON and allocates `(run_id, seq)` in the same write transaction.
  Run Console tails new SQLite rows after a monotonic sequence cursor, including cross-process
  writers, and deduplicates local-vs-polled delivery. JSONL is an atomic recovery/export surface;
  Doctor checks continuity, duplicates, terminal chains and export drift.
- Immutable forward-only migrations use `BEGIN IMMEDIATE`, SQLite online backup, integrity/FK
  checks, row-ID/count preservation, schema parity, and streaming Pydantic validation of every domain
  JSON record. The complete pending chain commits or rolls back as one transaction. Unknown/future
  revisions and current-domain corruption exit `2`; a pending-chain failure exits `3` with the
  preserved backup path.
- Git status/diff parsing is NUL-delimited and never stages, resets or otherwise mutates the user's
  index. Cleanup requires OpenAgent ownership metadata. Optional commits are limited to clean,
  OpenAgent-created worktrees; in-place user changes are never committed.
- Untrusted model/tool/event/CLI/provider/diff/projection surfaces have central byte/count limits.
  Limit handling is explicit (`truncated=true` or `output_limit_exceeded`), never silent.

## Reasoning privacy

**OpenAgent shows reasoning summaries. It never shows, requests, infers, or stores hidden
chain-of-thought.** The two are different things, and conflating them is how a tool ends up leaking
private model reasoning while believing it is being transparent.

* A **reasoning summary** is text the *backend itself* designates as user-visible. Codex emits these
  as `reasoning` items (short lines such as `**Checking git status and file contents**`), and its own
  event model defines that item as the reasoning *summary*. OpenAgent asks for them explicitly
  (`model_reasoning_summary`), because Codex emits none by default — which is why the previous
  version of this adapter, which threw the text away, left the user with nothing to go on.
* An **API agent** publishes its own progress through the `report_progress` and `update_plan` tools:
  explicit statements the model chooses to make to the user. The system prompt asks for them and
  tells the model, in those words, not to reveal private reasoning.
* Anything a provider does *not* designate as a user-visible summary — encrypted reasoning blobs, raw
  content parts, internal fields — is never mapped, never persisted, and never rendered. There is a
  regression test for exactly that.
* Reasoning **tokens** are counted (`reasoning_tokens` in usage). The reasoning they represent is
  never obtained.

Summaries pass through the same secret redaction and markup escaping as any other untrusted text
before they are persisted or rendered.

## UI injection

Every string that reaches a markup-enabled widget from outside — model messages, reasoning summaries,
questions, answers, commands, command output, tool names, file paths, provider/CLI errors, agent
titles and descriptions — is escaped through one shared helper (`tui/markup.safe_markup`), which also
strips ANSI and control characters. Unescaped, a model could render `[green]✓ tests passed[/green]`
as a real success line — including inside the very dialog asking whether to trust it.

## Reporting a vulnerability

Please open a private security advisory on the GitHub repository, or contact the maintainer directly
rather than filing a public issue. Include reproduction steps and the affected version.
