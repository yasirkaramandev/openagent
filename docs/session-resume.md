# Session resume (v0.2)

Resuming a conversation is not one operation. There are four mechanisms, they preserve different
amounts of the conversation, and they fail differently — so OpenAgent names them separately rather
than hiding them behind one "resume".

| Mode | Who holds the state | Preserves native state |
|---|---|---|
| `native_session` | a CLI runtime, by its own session id | yes |
| `server_interaction` | the provider, by interaction/response id | yes |
| `client_native_replay` | OpenAgent, as provider-native material | yes |
| `normalized_replay` | OpenAgent, as its own transcript | no |
| `unsupported` | nobody. Saying so is a feature. | — |

`normalized_replay` is always available, which is what makes refusing the stronger modes affordable.

## The failure this is built around

A resume is the one operation where *almost right* is the dangerous outcome.

Replaying DeepSeek's native assistant message into MiniMax is a malformed request. It errors
immediately, and that is fine — a loud failure is a cheap failure.

Resuming a conversation recorded against one model into a **different model works**. There is no
error. The model simply answers worse, and nobody can attribute the regression weeks later.

So [`services/resume.py`](../src/openagent/services/resume.py) separates two categories and never
conflates them.

### Blockers — refuse

Something that cannot produce a correct request:

- a **different provider** — the native material is addressed to someone else;
- a **different protocol** — recorded over one wire format, replayed into another;
- **crossing runtimes** — a CLI session id means nothing to an API provider, and the reverse;
- a **failed artifact integrity check** — a truncated native message does not error at the provider,
  it degrades the turn, which is exactly the unattributable failure;
- a **newer envelope schema** — written by a build this one does not understand;
- a **different project** — a project-scoped CLI session resumed from elsewhere would attach the run
  to somebody else's history. A boundary, not a degradation;
- a **missing runtime or provider** — there is nothing left to send anything to;
- an **expired remote session** — the id exists and its owner no longer has it.

### Warnings — ask

Something a human may legitimately accept, offered with three explicit choices — **migrate history**,
**start new**, **cancel**:

- a **changed model** (the dangerous-because-it-works case);
- a **changed model revision** — capabilities verified then are not evidence about it now;
- a **rotated credential** — provider-held state may no longer be visible to the new key;
- a **changed CLI version** — its session format may have changed with it;
- a **missing model** — blocks resuming with *that* model, not resuming at all.

A refusal still offers **start new** and **cancel**. A refusal with no way forward is a dead end.

### Absent provenance is "unknown", not a mismatch

A session recorded by an older build may carry no fingerprints. Treating that as a mismatch would make
every pre-v0.2 session unresumable, so a missing value is simply not compared.

## What is verified before a resume

```
runtime            provider           protocol          model
project fingerprint                   provider fingerprint
model fingerprint  session id         artifact hash     artifact schema
CLI version
```

## Continuation artifacts

Native payloads live under `<data dir>/sessions/<session-id>/`:

```
continuation.json     the native material, byte-exact
continuation.sha256   its hash, beside it as well as inside the envelope
metadata.json         provider, size, age, mode — so the session list never opens the payload
```

`0700` directories, `0600` files, atomic writes, symlink and traversal refusal, a size ceiling, and
hash verification on read. The path comes from `platformdirs` / `OPENAGENT_DATA_DIR`; there is no
literal `~/.openagent` anywhere.

**Secrets are handled by refusal, not redaction.** Replay needs byte fidelity, so editing the payload
to remove a key would corrupt the thing it exists to preserve. Instead the payload is scanned before
writing and a key-shaped token means the artifact is **not written** — the field is named, the value
never is, and normalized replay remains available. Fail-closed is the cheap option here.

The database stores a *reference* — path, hash, schema, mode, fingerprints — not the bytes. Bytes in
the row are what makes listing sessions slow, and the hash still makes truncation detectable.

## The turn terminal contract

Every resumed turn produces **exactly one `turn.started`** and **exactly one terminal result**.

A resumed turn is precisely where duplicate lifecycle events happen: the runtime announces a turn, the
provider reports the session already open, a replayed history produces a second completion. So
observations are collected and reconciled once, fail-closed, in the same direction as the run-level
contract:

```
cancelled  >  failed  >  completed
```

- no terminal observation at all → **failed**. A turn that never reported an outcome did not succeed,
  and recording it as completed is how a silently truncated turn becomes durable.
- two *different* terminals → the stronger one wins and the result is flagged `conflict`.
- the same terminal twice → collapses, and is **not** a conflict; the same outcome twice is not a
  disagreement.

## Per-runtime support

| Runtime | Mode | Notes |
|---|---|---|
| Codex | `native_session` | `--resume <id>` |
| Claude Code | `native_session` | `--resume <id>` |
| Gemini CLI | `unsupported` | no documented headless session contract |
| Qwen Code | `native_session` | only if the installed build advertises `--resume` |
| Kimi ACP | `native_session` | only if the agent advertises `loadSession` |
| Gemini API | `server_interaction` / `client_native_replay` | `store=True` gives the first; thought signatures make the second necessary |
| LM Studio, Qwen (Responses) | `server_interaction` | opt-in |
| DeepSeek, GLM, MiniMax, Ollama | `client_native_replay` | reasoning/thinking must be replayed with its tool call |
| Everything else | `normalized_replay` | always available |

Tested in [`tests/unit/test_session_resume.py`](../tests/unit/test_session_resume.py).
