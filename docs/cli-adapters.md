# CLI adapters (v0.2)

Six coding CLIs. An adapter runs an installed CLI as a subprocess and converts its native output into
OpenAgent's normalized events — it does not build an agent loop.

**Registration means the whole lifecycle exists.** `start_run`, `cancel`, `inspect_auth`,
`capabilities`, and the exactly-one-terminal-event contract. The wizard, Doctor, run preflight and the
executor all resolve through the registry, so half an adapter in it is an entry a user can select and
then watch fail.

## The two claims a capability makes

Every adapter reports two separate things and they are routinely conflated:

- **`structured_events`** — the CLI can report machine-readable results at all.
- **`live_structured_events`** — those results arrive *while the run proceeds*.

Gemini CLI has the first and not the second: it prints one JSON document when the run finishes.
OpenAgent could emit a synthetic `message.delta` per line to make the run console look busy, and does
not, because a fabricated delta is indistinguishable downstream from a real one — anything reasoning
about latency or partial output would be reasoning about a fiction.

## Status vocabulary

Six values, because there are six genuinely different situations. The middle four are the ones that
get collapsed into "installed":

| Status | Meaning |
|---|---|
| `verified-live` | the event mapping has been observed against a real run of this CLI |
| `fixture-validated` | mapping is validated against recorded fixtures only |
| `installed-version-unverified` | installed, but a different build from the one the mapping was captured against |
| `installed-auth-unknown` | installed, and the authentication state could not be determined |
| `installed-unsupported` | installed and not usable — selecting it would produce a run that cannot work |
| `not-installed` | the user does not have it. Not a failure. |
| `experimental` | fixture-validated and the contract may still be wrong |

## Discovery is not allowed to run your project's code

Probing a CLI means executing it, and a CLI executed inside a project reads that project's
configuration — hooks, extensions, MCP servers, agent definitions, any of which can run code. So
discovery runs **from an empty directory** with a **minimal environment**, and the only credentials in
scope are the ones *that CLI documents*.

A Gemini probe never receives `ANTHROPIC_API_KEY`. That is tested per CLI, against every other CLI's
variables, in [`tests/unit/test_cli_discovery_system.py`](../tests/unit/test_cli_discovery_system.py)
and again in the security audit.

---

## Per-CLI

| CLI | Executable | Class | Protocol | Resume | Sandbox |
|---|---|---|---|---|---|
| Codex | `codex` | first-class | stream-json | `--resume <id>` | `--sandbox` |
| Claude Code | `claude` | first-class | stream-json | `--resume <id>` | — |
| Gemini CLI | `gemini` | first-class | single-json | **unsupported** | `--sandbox` |
| Antigravity | `agy` | experimental | stream-json | session flag | — |
| Qwen Code | `qwen` | experimental | stream-json | `--resume` *if the build has it* | `--safe-mode` |
| Kimi ACP | `kimi` | experimental | JSON-RPC | `session/load` *if advertised* | — |

### Codex

- **Install** — npm, native, standalone release, Homebrew cask. Provenance decides whether OpenAgent
  may update it; a binary copied into `~/bin` has no mechanism that can be inferred.
- **Auth** — ChatGPT login or `OPENAI_API_KEY` / `CODEX_API_KEY`.
- **Model discovery** — the installed app-server's documented `model/list`.
- **Validated version** — `codex-cli 0.142.5`. A different installed build is reported as
  `installed-version-unverified`, not as verified.
- **Live verification** — `LIVE_VERIFIED` on 2026-07-27 against `codex-cli 0.145.0` with a ChatGPT
  login: a real run resolved to exactly one terminal event, emitted one `process.started` carrying a
  pid, produced assistant text, and cancelled cleanly.

### Claude Code

- **Install** — npm, native, Homebrew cask.
- **Auth** — `claude auth login`, or `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN`.
- **Model discovery** — documented aliases plus the project's own `.claude/settings.json`, including
  its `availableModels` policy list, scoped to the credential in play.
- **Live verification** — `BLOCKED_BY_AUTH` on 2026-07-27. Claude Code **2.1.214 is installed** on the
  verification machine and the account is **not signed in**, so the event mapping remains
  fixture-validated. The adapter is complete; the account is not usable. This is not a pass.

### Gemini CLI

- **Install** — npm (`@google/gemini-cli`), Homebrew.
- **Auth** — `GEMINI_API_KEY`, `GOOGLE_API_KEY`, or Vertex ADC variables. An interactive Google login
  leaves no variable, so absence is non-blocking: the CLI's own error is more useful than a guess.
- **`--output-format json` is probed, not assumed.** It is documented, and there are released versions
  where passing it produces `Unknown arguments: output-format` and the help text
  (google-gemini/gemini-cli#9009). A build without it is still usable, with `structured_events: false`.
- **Model discovery** — a settings policy allowlist first (an administrator has already answered the
  question with authority over the machine), then the credential's own API catalog, then nothing. No
  hardcoded alias list: a stale alias produces a run that fails for a reason the user cannot connect to
  anything they did.
- **Resume — unsupported, and that is a finding.** `/chat save` and `/chat resume` are *interactive*
  checkpoint commands; no headless session-id contract is documented. Re-sending prompt history is not
  native resume and must not be labelled as it — the CLI would start a new session with a longer
  prompt, which behaves differently and costs differently. `RESUME_SPIKE_REQUIRED` records what would
  have to be observed to change the answer.
- **Live verification** — `PROVIDER_UNAVAILABLE` (not installed on the verification machine).

### Antigravity

- **Install** — native / standalone release. **Auth** — Google account state under `~/.gemini`.
- **Model discovery** — `agy models`.
- **Live verification** — installed (1.1.7) and enumerating 11 models; the mapping was captured against
  1.1.0, so it reports `installed-version-unverified`.

### Qwen Code (experimental)

- **Install** — npm (`@qwen-code/qwen-code`).
- **Auth** — `DASHSCOPE_API_KEY` / `QWEN_API_KEY`, or an OpenAI-compatible endpoint + key pair.
- **Every flag is read out of the installed binary's `--help` before use.** Qwen Code is a Gemini-CLI
  fork whose releases carry no reliable relationship between a version string and a flag's presence,
  and a missing flag becomes `Unknown arguments` — a run that fails for no reason the user can connect
  to anything they did.
- **Discovery disables** hooks, extensions, skills, MCP, project agents and project memory. Listing
  models must not be able to execute project code.
- **Resume** — only when the installed build advertises `--resume`, and only within the same project
  fingerprint. Otherwise the run is refused: silently starting a fresh session would look like a resume
  and behave nothing like one.
- **Permissions** — `plan` / `read-only` / `safe-edit` / `full` map onto approval modes plus
  `--safe-mode`. `yolo` is deliberately not offered: a profile a user can pick from a list is a profile
  they will pick without reading what it does.
- **Live verification** — `PROVIDER_UNAVAILABLE` (not installed).

### Kimi ACP (experimental)

- **Protocol** — JSON-RPC over stdio. The agent is a **peer**, not a log producer, which brings failure
  modes a one-way JSONL reader does not have. All are handled in
  [`runtimes/cli/acp.py`](../src/openagent/runtimes/cli/acp.py): a per-message size ceiling, responses
  dispatched by id (an unknown id is dropped with a diagnostic rather than applied to whatever call is
  outstanding), monotonic request ids, stderr never parsed as protocol, and a bound on every call.
- **Capabilities come from the agent's own `initialize` handshake**, not from documentation. `resumable`
  is true only if the agent advertises `loadSession`.
- **Permission prompts arrive as requests to us** and are answered from the run's profile using an
  allowlist. Answering "allow" by default would make every permission profile decorative; an
  unrecognised tool kind is refused, because a tool nobody classified is a tool nobody reviewed.
- **Model discovery** — ACP does not carry a model list, and none is invented. The wizard keeps the
  manual-id path open.
- **Live verification** — `PROVIDER_UNAVAILABLE` (not installed).

---

## Cancellation

Every adapter cancels through a process tree whose identity is verified by pid, create time and
executable before anything is signalled, so a reused pid cannot cause an unrelated process to be
killed. Graceful signal first, then a timeout, then forced termination. Kimi ACP additionally sends a
protocol-level `session/cancel` first, so the agent can finish whatever it was mid-way through.

## Updates

Updates go through one provenance-checked updater shared with the rest of the project. npm-global
installs update with npm, Homebrew with brew, and a manually placed binary has **no** update mechanism
OpenAgent will infer — guessing one is how a working install becomes a broken one. An update is
refused while a run is using that CLI.

## Testing

- [`tests/contract/test_cli_contract.py`](../tests/contract/test_cli_contract.py) — the §25 battery,
  parameterized across all six.
- [`tests/unit/test_cli_discovery_system.py`](../tests/unit/test_cli_discovery_system.py) — descriptors,
  status classification, credential isolation.
- [`tests/live/test_cli_live_verification.py`](../tests/live/test_cli_live_verification.py) — opt-in
  (`OPENAGENT_LIVE_CLI_TESTS=1`), real runs, honest skips.
