# Add-Agent wizard v2

Twelve steps. Steps that do not apply are not shown — a CLI agent has no region or model-catalog step
of its own, and an API agent has no sandbox step because there is no subprocess to sandbox.

```
1 Runtime   2 Provider/CLI   3 Auth      4 Region/server
5 Model discovery            6 Capability filtering
7 Permission                 8 Sandbox   9 Resume
10 Live probe               11 Review   12 Create
```

The decisions live in [`tui/wizard_v2.py`](../src/openagent/tui/wizard_v2.py), separately from the
Textual screen, so they can be tested without driving widgets.

## A badge names its evidence

"Tools" lit because OpenRouter's catalog said so and "Tools" lit because a probe watched it happen are
different claims. Someone choosing a model for an agent that *must* call tools needs to know which one
they have.

- **Verified** = probe-backed and not aged. Shown as verified.
- **Lit but unverified** = a catalog claim, a fixture, or a curated preset. Shown as lit, with a
  tooltip saying where it came from.
- **Unknown** = nothing has established it. Shown, not omitted — a missing badge is indistinguishable
  from an unsupported one at a glance, and "we do not know" is the most common truthful answer.

Every tooltip names its source in a sentence, because a user deciding whether to trust a badge is
deciding whether to trust that sentence:

```
Tools: supported — verified by a live probe against this credential; observed 2026-07-27
Tools: supported — reported by the provider's catalog, not verified here
Tools: not established for this model. Run the capability probe to settle it.
```

Probe evidence older than 30 days stops counting as verified and says so.

## A catalog that cannot be read is not an empty catalog

Presenting an empty list as authoritative is how a working provider looks broken. Four explicit ways
forward, because they address genuinely different situations:

| Option | For |
|---|---|
| Retry | a transient failure |
| Use cached catalog | an outage, when a cache exists |
| Enter model ID manually | a provider with no catalog at all |
| Use provider default | a user who does not care which model |

A **partial** catalog shows what it has and says it is incomplete. A **manual-only** provider is
presented as a configuration, not a failure.

## The probe gate gates

If the user marks a capability as required — tools, reasoning, vision — and the probe did not
establish it, creation does not silently proceed. It needs an explicit override, and the override
reason is recorded on the agent.

The two failing cases are kept apart because they lead to different decisions:

- **UNSUPPORTED** — "this model does not support tools, which you marked as required. Creating the
  agent anyway means it will fail at the first attempt to use it."
- **UNKNOWN** — "tool calling could not be verified for this model. It may work; nothing here has seen
  it work."

## Server-side state is disclosed where it is chosen

Not in a settings page nobody opens. The step that enables it is the step that says what it means, in
concrete words — "Google will retain this conversation", not "state is stored remotely". The abstract
phrasing is what lets someone enable it without registering what it means.

An insecure (plain-HTTP, non-loopback) endpoint gets its own disclosure: the API key and every prompt
cross the network in cleartext.

## Review, and Create

The review step lists verified capabilities separately from merely-claimed ones, shows every
disclosure the configuration owes, and names the **credential source** — never the credential. The
review projection has no field a secret could occupy.

Create is transaction-safe. If the credential is written and the database write then fails, the
existing recovery system reconciles it; a credential without its provider row is exactly the state
that recovery exists for.
