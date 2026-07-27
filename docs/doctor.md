# Doctor

`openagent doctor` runs local, offline checks. Live provider network tests are excluded on purpose so
it stays fast and usable without credentials.

```bash
openagent doctor                    # grouped into sections
openagent doctor --flat             # one line per check, for scripts
openagent doctor --json             # sections + the flat check list
openagent doctor --refresh-cli-updates
```

## Why sections

A flat list is readable at ten checks and stops being readable at sixty, which is where v0.2 lands it
— nine providers, six CLIs, per-model capability evidence, session artifacts. Worse, a flat list has
no way to say "the database is fine and the providers are not": every line has equal weight, so the
one that matters is the one you happen to read.

Eleven sections, each carrying **its own worst status**. A green section summary over one failing row
is the specific thing this must not do.

| Section | What it answers |
|---|---|
| Installation | version, commit, channel, provenance, PATH winner, shadowed binaries |
| Database | schema revision, integrity_check, foreign_key_check, backups, pending migrations, leases |
| Credentials | which credential each provider resolves to, and its revision |
| Providers | endpoint, TLS, connection, catalog state and freshness, region/workspace, local/remote, server-state |
| Models | model id, context, capabilities **with evidence source and age**, probe version, deprecation |
| CLI adapters | executable, shadows, version vs validated version, auth, models, protocol, live streaming, sandbox, resume, update |
| Sessions | runtime/provider/model still exist, remote id, artifact + hash + schema, project fingerprint, CLI version |
| Workspaces | git repository, worktree isolation, OPENAGENT.md sync |
| Security backend | OS keychain availability |
| Updater | OpenAgent's own update state |
| Release channel | which channel this install follows |

Two rules:

- **Every check lands in exactly one section.** A check with no section is invisible in a sectioned
  view, which is worse than a noisy flat list. Anything unrecognised goes to Installation and is
  reported as unclassified rather than dropped.
- **An empty section says "nothing to report".** "No providers configured" is a finding; an absent
  section reads as "we did not look".

## Statuses that are deliberately not failures

- **A CLI that is not installed is `ok`.** The user simply does not have it. Reporting that red trains
  people to ignore red.
- **A provider with no listable catalog is `ok`.** Manual model entry is a supported configuration.
- **An endpoint with no `/models` route is `ok`.** It answered; it just does not list models.

## Statuses that are failures

- **Plain HTTP at a non-loopback address** — the credential is crossing the network in cleartext. That
  is not a thing to note in passing.
- **An unreadable catalog** — distinct from an empty one.
- **A CLI installed and unusable** — the wizard would offer it and it cannot work.

## Capability evidence and staleness

Doctor shows each capability with its **source** (`live_probe`, `provider_catalog`,
`verified_fixture`, `curated_preset`, `manual_override`), when it was observed, and the probe version
that produced it.

Probe evidence older than 30 days is reported as aged. **It is never refreshed automatically** —
re-probing spends the user's quota, and doing that as a side effect of running a diagnostic is not
Doctor's decision to make. A *catalog* claim does not go stale by sitting there; only an observation
does.

## Exit codes

`0` clean, `1` warnings, `2` a failure that needs attention.

Note that `openagent doctor` exiting `1` on warnings is normal, and any CI step consuming it must
handle that deliberately.
