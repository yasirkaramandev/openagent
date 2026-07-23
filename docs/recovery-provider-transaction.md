# Provider transaction & recovery state machine

Adding a provider touches three durable stores that cannot be committed atomically together: the OS
keychain (the secret), the SQLite `provider_connections` row, and a compensating-operation journal
file on disk. A crash or an I/O failure can land between any two of them. This document describes the
state machine that lets startup recovery finish an interrupted add **without ever deleting a provider
the user durably created** — the guiding rule being:

> Preserving verified, committed data matters more than automatically removing a possible orphan.
> On any ambiguity, recovery preserves and surfaces the situation through Doctor.

## Why a durable commit marker exists

The row is inserted (and committed) inside `ProviderTransaction.__enter__`, at journal stage
`db_written`. The transaction is only *logically* complete later, when `commit()` runs. Before the
fix, `commit()` marked itself committed **only in memory** and then unlinked the journal file as its
last act — writing no durable "this add committed" marker. So two very different situations were
indistinguishable on the next startup:

- a **never-committed** add whose partner write failed (should be rolled back), and
- a **committed** add whose journal file simply had not been unlinked yet — because the process was
  killed, or because the fallible legacy-secret cleanup inside `commit()` raised before the unlink.

Both left a pending operation at `db_written` with the live row's `credential_revision` equal to the
journal's. Recovery deleted on that equality alone, so it destroyed committed providers (and their
model probes and new secret). The fix adds a durable `commit_durable` marker written **before** the
fallible cleanup, and a `rollback_pending` marker written at the top of the rollback path, so the two
cases are no longer ambiguous.

## Stages

| Stage | Meaning | Recovery treats it as |
|---|---|---|
| `begun` | operation opened | (pre-durable; normally superseded by later stages) |
| `secret_write_intent` / `secret_written` | keychain write in progress / done | pre-durable |
| `db_written` | row inserted & committed; commit-vs-rollback **not yet decided** | **ambiguous → preserve** |
| `commit_durable` | `commit()` reached durability, before legacy-secret cleanup | **committed → preserve** |
| `legacy_cleanup_pending` | committed, but the old pre-revision secret was not yet removed | **committed → preserve; retry legacy cleanup only** |
| `rollback_pending` | uncommitted `__exit__` began compensating | **rollback → may delete the owned generation** |
| `owned_compensation` | the owned generation was deleted during rollback | **rollback → finish/verify** |
| `superseded_generation` | the live row belongs to a newer generation | never touch the live row; clean only this op's own scoped secret |
| `recovery_ambiguous` | recovery could not prove commit or rollback; preserved on purpose | preserved; reported by Doctor |

`db_written` is deliberately **not** proof that a row may be deleted. Only `rollback_pending` /
`owned_compensation` are, and they are written *before* any compensation runs.

## Happy path

```
__enter__:  begun → [secret_write_intent → secret_written] → db_written   (row committed)
commit():   (mark committed) → commit_durable → delete legacy secret → complete()  (journal unlinked)
```

If the legacy-secret cleanup fails, `commit()` does **not** undo the committed provider. It advances
to `legacy_cleanup_pending` and returns; the add still succeeds. The leftover is only the user's
*old* pre-revision secret, and recovery/Doctor finish it later.

## Rollback path (uncommitted `__exit__`)

```
__exit__ (not committed):  rollback_pending
    → restore keychain exactly
    → delete_owned_with_probes(id, revision)
        deleted    → owned_compensation
        not deleted→ superseded_generation   (a newer generation already owns the id)
    → complete() when restore and compensation both succeeded, else leave pending
```

## Startup recovery decision (`OpenAgentApp._recover_operations`)

For a `provider_add` operation, keyed off the stage:

- **`commit_durable` / `legacy_cleanup_pending`** — the provider is committed. Never delete it or its
  new credential; only retry deleting the legacy secret, then complete.
- **`rollback_pending` / `owned_compensation`** — proven rollback. Delete the owned generation *only*
  while the live row still belongs to that same `credential_revision`; then clean the scoped secret.
- **`db_written` or any unknown/legacy stage** — ambiguous. Preserve on doubt:
  - no row → clean only this operation's own orphaned scoped secret and complete;
  - live row of a *different* revision → `superseded_generation`; clean only this op's scoped secret;
  - live row of the *same* revision → cannot prove rollback, so **preserve the row and its
    credential** (and the legacy secret), advance to `recovery_ambiguous`, and keep the operation
    pending for Doctor.

A `provider_add` journal entry from an older build (no revision/stage) that points at a live row has
no ownership proof and is treated the same way: preserved and reported, never guessed.

## Visibility

`openagent doctor` reports each still-pending operation by a redacted recovery state — `provider
rollback pending`, `provider legacy credential cleanup pending`, `provider recovery ownership
ambiguous`, `provider generation superseded` — with counts only. The journal never stores secret
values, and Doctor renders no payload (which can carry a credential ref, header or URL). When an
operation is stuck, the safe, existing repair surfaces are `openagent doctor --json` and
`openagent update --repair`.
