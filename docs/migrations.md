# Migrations

Forward-only, numbered, one transaction each, with an online backup taken before any pending revision
is applied. `run_migrations` refuses to open a database written by a newer OpenAgent rather than
writing against a schema it does not understand.

## Chain

```
0001 … 0013   shipped on main
0014          provider generation ownership — on the 0.1.6 release branches
0015 0016 0017  v0.2 — written and tested, deliberately NOT chained yet
```

## Why 0015–0017 are held back

Revision 0014 is on the 0.1.6 release branches and has not reached `main`.

Appending 0015 with `down_revision="0014"` onto a chain that ends at 0013 produces a chain with a
**hole**, and `run_migrations` walks straight into it — on a user's database. Cherry-picking a copy of
0014 onto this branch instead would create a *second* 0014 that conflicts with the real one the moment
the release merges. Both mistakes land in the one place that cannot be taken back.

So [`storage/migrations_v2.py`](../src/openagent/storage/migrations_v2.py) declares the fragment and
`register_v2_migrations()` splices it on **only when 0014 is present**. Nothing about the bodies is
provisional — they are complete and their tests apply them to a real database. When 0014 lands, the
registration is a decision, not a piece of work.

Doctor reports the hold and its reason under **Database**, so an operator can see it without reading
source.

## What each revision does

### 0015 — provider protocol, discovery, region, locality

Promotes `protocol`, `model_discovery`, `region`, `workspace_id`, `server_state_enabled`, `is_local`
and `profile_version` from the JSON blob to columns.

Not tidying: these are what the provider list, Doctor and the wizard *filter* on, and a JSON blob
cannot be filtered without reading and parsing every row. The region column is load-bearing — a key
belongs to exactly one region and the endpoint is chosen from it.

- The backfill reads each row's own JSON. Nothing is invented.
- `server_state_enabled` is **never inferred as on**. A row written before the column existed did not
  have it enabled, whatever its provider is capable of.
- Locality is decided by the **parsed host**, so `https://localhost.attacker.example` is correctly not
  loopback.
- A row naming a protocol this build does not implement **blocks** the upgrade, named by hash rather
  than by id, with the backup retained. Only the user knows what that row was meant to be.

### 0016 — capability evidence

Adds a `capability_evidence` table: provider, model, capability, status, source, `observed_at`,
`probe_version`, `provider_version`, `model_revision`, `credential_revision`.

The v0.1 shape was booleans on a model row. That cannot express "supported, by a live probe, against
revision X, at time T" — and without those columns there is no way to invalidate exactly the rows a
credential rotation invalidates.

- Legacy booleans migrate with source **`legacy_migration`** and `probe_version = 0`.
  **Not `live_probe`.** That is the strongest automatic source; labelling a v0.1 guess with it would
  let it outrank every real catalog reading from then on.
- A **null** legacy flag writes **no row**. `None` means "never determined", and a row would record
  knowledge that does not exist.
- `vision` migrates to the v0.2 capability name `image_input`.
- Deleting a provider cascades its evidence: a claim about a model reached through a deleted
  credential is not evidence any more.

### 0017 — session resume

Adds `resume_mode`, `remote_session_id`, `remote_interaction_id`, `continuation_path`,
`continuation_hash`, `continuation_schema`, `runtime_version`, and the `provider_`/`model_`/`project_`
fingerprints to `runs`.

Every column is something `verify_resume` compares. The artifact's **path and hash** are stored rather
than its bytes: bytes in the row are what makes listing sessions slow, and the hash still makes a
truncated artifact detectable.

A run that already carried a CLI session id is backfilled as `native_session` — it was resumable by
that id all along; writing the mode down makes the path explicit instead of re-derived.

## Testing

Every revision is exercised against a real SQLite database built by the real base chain, plus a
fixture standing in for 0014's column:

- schema shape and backfill correctness
- idempotency (each applies twice with no change)
- row count and row identity preserved
- `PRAGMA integrity_check` and `PRAGMA foreign_key_check` clean
- a malformed row blocks the upgrade **loudly**, without disclosing the row's id
- a mid-chain failure rolls the whole transaction back

See [`tests/unit/test_migrations_v2.py`](../tests/unit/test_migrations_v2.py).

## When 0014 lands on main

1. Merge `main` into the v0.2 integration branch.
2. `register_v2_migrations()` starts returning `True` on its own — the hold is a condition, not a flag.
3. The test asserting the hold fails, which is the signal to update it.
4. Run the migration suite against a copy of a real 0.1.6 database.
