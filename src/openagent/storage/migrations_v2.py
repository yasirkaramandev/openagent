"""Migrations 0015–0017: the v0.2 schema (spec §23).

These three revisions are written, tested, and **deliberately not chained into**
:data:`~.migrations.MIGRATIONS` on this branch. The reason is mechanical: revision 0014 lives on the
0.1.6 release branches and has not reached ``main`` yet. Appending 0015 with ``down_revision="0014"``
to a chain that ends at 0013 would produce a chain with a hole, and cherry-picking a copy of 0014 here
would create a *second* 0014 that conflicts with the real one the moment the release merges. Either
way the damage lands on a user's database, which is the one place a mistake cannot be taken back.

So the chain fragment is declared here and :func:`register_v2_migrations` splices it on only when 0014
is actually present. Nothing about the migration bodies is provisional — they are complete and their
tests apply them against a real database — and the day 0014 reaches ``main`` the registration becomes a
no-op decision rather than a piece of work.

What each revision adds, and why it is a schema change rather than a JSON field:

* **0015** — protocol, discovery strategy, region, workspace, server-state preference, local/remote.
  These are the columns provider *queries* filter on: "which connections are local", "which speak
  Anthropic Messages", "which region is this key for". In the JSON blob they are unqueryable, and the
  region one is load-bearing — a key belongs to exactly one region and the endpoint is chosen from it.
* **0016** — capability evidence with its provenance. The v0.1 shape was booleans on a model row, which
  cannot express "supported, by a live probe, against revision X, at time T". Without those columns
  there is no way to invalidate exactly the rows a credential rotation invalidates.
* **0017** — resume mode, session/interaction ids, the continuation artifact's path, hash and schema,
  and the four fingerprints resume verification compares. A resume that cannot check what it is
  resuming into is the failure mode :mod:`openagent.services.resume` exists to prevent.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .migrations import (
    MIGRATIONS,
    Migration,
    MigrationVerificationError,
    _add_column,
    _column_exists,
    _redacted_record_id,
    _table_exists,
)

#: The revision these three build on. Not a guess — 0014 is "provider generation ownership", and 0015
#: adds columns to the same ``provider_connections`` table it touched.
BASE_REVISION = "0014"

_FORWARD = (
    "Local user data revisions are forward-only; restoration uses the reported online backup."
)


# --------------------------------------------------------------------------- 0015


#: Protocol values a provider row may carry. Validated on write rather than trusted: an unrecognised
#: protocol is a row nothing can build an adapter for, and finding that out at run time turns a data
#: problem into a mysterious runtime failure.
_KNOWN_PROTOCOLS = frozenset(
    {
        "openai-chat",
        "openai-responses",
        "anthropic-messages",
        "gemini-interactions",
        "ollama-native-chat",
        "lmstudio-native-chat",
    }
)

_KNOWN_DISCOVERY = frozenset(
    {
        "openai-models",
        "gemini-models",
        "openrouter-catalog",
        "ollama-tags-show",
        "lmstudio-native",
        "curated-catalog",
        "manual-only",
    }
)


def _m0015_provider_protocol_and_region(conn: Connection) -> None:
    """Promote protocol, discovery, region, workspace and locality to columns (spec §23.1).

    All five already exist inside ``provider_connections.data``. Promoting them is not tidying: they
    are what the provider list, Doctor and the wizard *filter* on, and a JSON blob cannot be filtered
    without reading every row and parsing it.

    The backfill reads each row's existing JSON, so no value is invented. A row whose JSON does not
    name a protocol keeps the historical default (``openai-chat``) — that is what the code has been
    doing with it all along, so writing it down changes nothing about behaviour and makes it visible.
    """

    if not _table_exists(conn, "provider_connections"):
        return

    _add_column(conn, "provider_connections", "protocol", "VARCHAR NOT NULL DEFAULT 'openai-chat'")
    _add_column(
        conn,
        "provider_connections",
        "model_discovery",
        "VARCHAR NOT NULL DEFAULT 'openai-models'",
    )
    _add_column(conn, "provider_connections", "region", "VARCHAR")
    _add_column(conn, "provider_connections", "workspace_id", "VARCHAR")
    #: 0 = the provider keeps no state for us. Default off, everywhere it exists: turning it on is a
    #: privacy decision the user makes knowingly (spec §10.4, §13.3, §16.5).
    _add_column(conn, "provider_connections", "server_state_enabled", "INTEGER NOT NULL DEFAULT 0")
    #: 1 = a service the user runs. Decides the loopback plain-HTTP exemption, so it is a column and
    #: not a string comparison against the URL at every call site.
    _add_column(conn, "provider_connections", "is_local", "INTEGER NOT NULL DEFAULT 0")
    #: Which CompatibilityProfile shape produced this row's behaviour, so a profile change can be
    #: reasoned about after the fact.
    _add_column(conn, "provider_connections", "profile_version", "VARCHAR NOT NULL DEFAULT '2'")

    invalid: list[str] = []
    for row in conn.exec_driver_sql(
        "SELECT id, provider_type, data FROM provider_connections"
    ).mappings():
        payload = _payload(row["data"])
        protocol = _text(payload.get("protocol")) or "openai-chat"
        if protocol not in _KNOWN_PROTOCOLS:
            invalid.append(str(row["id"]))
            continue
        discovery = _discovery_for(str(row["provider_type"] or ""), payload)
        region = _text(payload.get("region"))
        workspace = _text(payload.get("workspace_id"))
        # Never inferred as on. A row written before this column existed did not have server state
        # enabled, whatever its provider is capable of.
        state = payload.get("server_state_enabled") is True
        local = _looks_local(payload)

        # Relational columns and the JSON aggregate are written in the same statement, because
        # they are two encodings of one fact and this migration is the moment both are set.
        #
        # This was previously left undone: ProviderConnection was extra="forbid" and declared none
        # of these fields, so writing them into `data` made every provider row unparseable and
        # rolled the migration back. The domain model now declares them, so the projection is
        # possible and the invariant holds.
        #
        # `is_local` is deliberately absent from the JSON: it is a derived property on the domain
        # model, not stored state. Persisting it would create a second, settable source of truth
        # for a flag that decides the plaintext-HTTP exemption.
        payload["protocol"] = protocol
        payload["model_discovery"] = discovery
        payload["region"] = region
        payload["workspace_id"] = workspace
        payload["server_state_enabled"] = state
        payload["profile_version"] = "2"

        conn.execute(
            text(
                "UPDATE provider_connections SET protocol=:protocol, model_discovery=:discovery, "
                "region=:region, workspace_id=:workspace, server_state_enabled=:state, "
                "is_local=:local, profile_version='2', data=:data WHERE id=:id"
            ),
            {
                "protocol": protocol,
                "discovery": discovery,
                "region": region,
                "workspace": workspace,
                "state": 1 if state else 0,
                "local": 1 if local else 0,
                "data": json.dumps(payload, ensure_ascii=False),
                "id": row["id"],
            },
        )

    if invalid:
        listed = ", ".join(_redacted_record_id(value) for value in sorted(invalid))
        raise MigrationVerificationError(
            "these provider connections name a wire protocol this build does not implement, so the "
            f"protocol column cannot be populated for them: {listed}. Fix or remove them and retry; "
            "the pre-migration backup is retained"
        )


#: Discovery strategy per provider type, for the backfill. Only providers whose strategy is *not* the
#: OpenAI convention are listed — everything else keeps the default, which is what it was already
#: doing.
_DISCOVERY_BY_TYPE = {
    "gemini": "gemini-models",
    "openrouter": "openrouter-catalog",
    "ollama": "ollama-tags-show",
    "lmstudio": "lmstudio-native",
    "qwen": "curated-catalog",
}


def _discovery_for(provider_type: str, payload: dict[str, Any]) -> str:
    explicit = _text(payload.get("model_discovery"))
    if explicit and explicit in _KNOWN_DISCOVERY:
        return explicit
    return _DISCOVERY_BY_TYPE.get(provider_type, "openai-models")


#: Hosts that make a connection local. Matched on the parsed host, never as a substring of the URL:
#: ``https://localhost.attacker.example`` contains "localhost" and is not loopback.
def _looks_local(payload: dict[str, Any]) -> bool:
    """Locality, decided by the one helper every other caller uses.

    This used to carry its own ``_LOOPBACK`` set, and that set contained ``0.0.0.0`` — which is
    not loopback. It is the *unspecified* address, the wildcard a server binds to in order to
    accept traffic from every interface, and it is the host a user is most likely to paste having
    read ``Listening on 0.0.0.0:11434`` in a server log. So the one host that most strongly
    suggests the service is reachable from off-box was being marked local and handed the
    plaintext-HTTP exemption. The same set matched only ``127.0.0.1`` while the whole
    ``127.0.0.0/8`` block is loopback.

    A migration having a second, quietly different opinion about a security classification is the
    problem here, not the particular entries. ``is_loopback`` is the authority.
    """

    from ..providers.spec import is_loopback

    return any(
        is_loopback(url)
        for url in (_text(payload.get("base_url")), _text(payload.get("anthropic_base_url")))
        if url
    )


# --------------------------------------------------------------------------- 0016


_EVIDENCE_TABLE_DDL = """
CREATE TABLE capability_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id VARCHAR NOT NULL REFERENCES provider_connections (id) ON DELETE CASCADE,
    model_id VARCHAR NOT NULL,
    capability VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    source VARCHAR NOT NULL,
    observed_at VARCHAR NOT NULL,
    probe_version INTEGER NOT NULL DEFAULT 1,
    provider_version VARCHAR,
    model_revision VARCHAR,
    credential_revision VARCHAR NOT NULL DEFAULT '',
    -- The four columns below are what makes invalidation *targeted* rather than a blunt purge.
    -- Evidence is only valid for the endpoint that produced it: the same model id behind a
    -- different protocol, base URL, region or workspace is a different question, and answering it
    -- from a cached row is how a capability the user does not have gets reported as supported.
    -- Without these stored, "invalidate on base URL change" cannot be expressed as a query, so
    -- the rule would exist in prose only.
    protocol VARCHAR NOT NULL DEFAULT '',
    -- The base URL is fingerprinted, not stored: it can carry a key in a query string or a
    -- tenant name in a host, and this table is read by Doctor and printed.
    base_url_fingerprint VARCHAR NOT NULL DEFAULT '',
    region VARCHAR,
    workspace_id VARCHAR,
    detail VARCHAR NOT NULL DEFAULT '',
    -- Deduplication is an explicit fingerprint column, not a UNIQUE over the identity columns.
    --
    -- SQLite never treats two NULLs as equal in a UNIQUE constraint, and region, workspace_id and
    -- model_revision are all nullable. A UNIQUE spanning them therefore deduplicates nothing for
    -- exactly the rows most likely to repeat — re-running the migration inserted the legacy rows a
    -- second time, because every NULL made each row "distinct" from its own twin.
    --
    -- observation_key COALESCEs the whole identity into one NOT NULL string, so byte-identical
    -- re-observations collide (making the backfill a genuine no-op) while two facts that differ in
    -- any dimension -- a rotated credential, another region, a newer probe -- stay two rows.
    observation_key TEXT NOT NULL,
    UNIQUE (observation_key)
)
"""

#: The legacy capability booleans, and the capability each maps to. A row rewritten from these is
#: evidence of the *weakest* kind: it records what an older build believed, not something observed
#: under the current probe definition.
_LEGACY_CAPABILITY_FIELDS = {
    "text": "text",
    "streaming": "streaming",
    "tool_calling": "tool_calling",
    "parallel_tool_calling": "parallel_tool_calling",
    "structured_output": "structured_output",
    "vision": "image_input",
    "system_prompt": "system_prompt",
}

#: The source a migrated legacy boolean gets. Emphatically **not** ``live_probe``: the old row does not
#: record whether anything was probed, under which probe version, or against which model revision, and
#: labelling it as a live probe would let a v0.1 guess outrank a real v0.2 catalog reading forever.
LEGACY_SOURCE = "legacy_migration"


def _m0016_capability_evidence(conn: Connection) -> None:
    """Give capability claims a provenance (spec §23.2).

    The v0.1 shape is booleans inside ``models.data.capabilities`` plus a single
    ``capabilities_tested_at``. That cannot express the four things v0.2 decides with: which source
    said so, under which probe definition, against which model revision, and with which credential. A
    ledger needs all four, because the point of ranking evidence is to invalidate exactly the rows a
    change invalidates — and a boolean has nothing to invalidate on.

    Legacy booleans are carried across as ``legacy_migration``, which is the honest source. Recording
    them as ``live_probe`` would be a lie with consequences: it is the strongest automatic source, so a
    v0.1 guess would outrank every real catalog reading from then on.
    """

    if not _table_exists(conn, "capability_evidence"):
        conn.exec_driver_sql(_EVIDENCE_TABLE_DDL)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_capability_evidence_model "
            "ON capability_evidence (provider_id, model_id)"
        )

    if not _table_exists(conn, "models"):
        return

    # A model row's provider column has been named differently across revisions; resolve it rather
    # than assuming, because guessing wrong here silently migrates zero rows.
    provider_column = next(
        (
            candidate
            for candidate in ("provider_connection", "provider_id")
            if _column_exists(conn, "models", candidate)
        ),
        None,
    )
    if provider_column is None:
        return

    # The provider's own endpoint identity, so a legacy capability row records the endpoint it was
    # actually observed against (spec §8.8). Recording blanks would leave it looking valid against
    # every endpoint forever, which is the opposite of what the scope columns are for.
    known_providers = {
        str(row["id"]): {
            "credential_revision": row["credential_revision"] or "",
            "protocol": row["protocol"] or "",
            "base_url_fingerprint": _base_url_fingerprint(_payload(row["data"])),
            "region": row["region"],
            "workspace_id": row["workspace_id"],
        }
        for row in conn.exec_driver_sql(
            "SELECT id, credential_revision, protocol, region, workspace_id, data "
            "FROM provider_connections"
        ).mappings()
    }

    for row in conn.exec_driver_sql(
        f"SELECT id, {provider_column} AS provider, data FROM models"
    ).mappings():
        provider_id = str(row["provider"] or "")
        if provider_id not in known_providers:
            # The foreign key would reject it. A model row whose provider is gone is a pre-existing
            # inconsistency, not something this migration should fail the whole upgrade over.
            continue
        payload = _payload(row["data"])
        capabilities = payload.get("capabilities")
        if not isinstance(capabilities, dict):
            continue
        # NULL, never "". A legacy row genuinely does not know when it was observed, and an empty
        # string in a timestamp column is not "unknown" -- it is a parse error handed to whoever
        # reads it next.
        observed_at = _text(payload.get("capabilities_tested_at"))
        remote_model = _text(payload.get("remote_model_id")) or str(row["id"])

        for legacy_field, capability in _LEGACY_CAPABILITY_FIELDS.items():
            value = capabilities.get(legacy_field)
            if value is None:
                # None means "never determined" in the v0.1 model too. Writing a row for it would
                # turn an absence of knowledge into a record of one.
                continue
            scope = _provider_scope(known_providers, provider_id)
            values = {
                "provider": provider_id,
                "model": remote_model,
                "capability": capability,
                "status": "supported" if value is True else "unsupported",
                "source": LEGACY_SOURCE,
                "observed": observed_at,
                # probe_version 0 marks "produced by no probe definition we still run", so a
                # freshness check treats it as stale rather than current.
                "credential": scope["credential_revision"],
                "protocol": scope["protocol"],
                "fingerprint": scope["base_url_fingerprint"],
                "region": scope["region"],
                "workspace": scope["workspace_id"],
                "detail": "migrated from a pre-v0.2 capability flag",
            }
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO capability_evidence "
                    "(provider_id, model_id, capability, status, source, observed_at, "
                    " probe_version, model_revision, credential_revision, protocol, "
                    " base_url_fingerprint, region, workspace_id, detail, observation_key) "
                    "VALUES (:provider, :model, :capability, :status, :source, :observed, "
                    " 0, NULL, :credential, :protocol, :fingerprint, :region, :workspace, "
                    " :detail, :key)"
                ),
                {
                    **values,
                    "key": observation_key(
                        provider_id=provider_id,
                        model_id=remote_model,
                        capability=capability,
                        source=LEGACY_SOURCE,
                        protocol=scope["protocol"],
                        base_url_fingerprint=scope["base_url_fingerprint"],
                        credential_revision=scope["credential_revision"],
                        region=scope["region"],
                        workspace_id=scope["workspace_id"],
                        probe_version=0,
                        model_revision=None,
                    ),
                },
            )


def observation_key(
    *,
    provider_id: str,
    model_id: str,
    capability: str,
    source: str,
    protocol: str,
    base_url_fingerprint: str,
    credential_revision: str,
    region: str | None,
    workspace_id: str | None,
    probe_version: int,
    model_revision: str | None,
) -> str:
    """A deterministic identity for one observation, as a NOT NULL string.

    Exists because SQLite never treats two NULLs as equal in a UNIQUE constraint, and three of the
    identity columns are nullable. A UNIQUE spanning them deduplicates nothing precisely for the
    rows most likely to repeat, so re-running the backfill inserted every legacy row a second time.

    Collapsing the identity into one string makes the comparison explicit rather than dependent on
    SQL NULL semantics: byte-identical re-observations collide, and two facts differing in any
    dimension stay two rows.
    """

    parts = (
        provider_id,
        model_id,
        capability,
        source,
        protocol,
        base_url_fingerprint,
        credential_revision,
        region or "",
        workspace_id or "",
        str(probe_version),
        model_revision or "",
    )
    return "\x1f".join(parts)


def _base_url_fingerprint(payload: dict[str, Any]) -> str:
    """A stable, secret-free identifier for an endpoint.

    The URL itself is never stored in evidence: it can carry a key in a query string or a tenant
    name in a host, and this table is read by Doctor and printed. Scheme+host+port is enough to
    tell two endpoints apart, which is all the scope check needs.
    """

    from hashlib import sha256
    from urllib.parse import urlsplit

    url = _text(payload.get("base_url")) or _text(payload.get("anthropic_base_url"))
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    origin = f"{parts.scheme}://{(parts.hostname or '').lower()}:{parts.port or ''}"
    return sha256(origin.encode("utf-8")).hexdigest()[:32]


def _provider_scope(known: dict[str, Any], provider_id: str) -> dict[str, Any]:
    """The endpoint identity a legacy capability row inherits from its provider (spec §8.8).

    Legacy evidence was observed against whatever endpoint that provider pointed at, so recording
    the provider's actual protocol/region/credential is what makes the row invalidatable later.
    Recording blanks would make it look valid against every endpoint forever.
    """

    row = known.get(provider_id) or {}
    return {
        "credential_revision": row.get("credential_revision") or "",
        "protocol": row.get("protocol") or "",
        "base_url_fingerprint": row.get("base_url_fingerprint") or "",
        "region": row.get("region"),
        "workspace_id": row.get("workspace_id"),
    }


# --------------------------------------------------------------------------- 0017


_RESUME_COLUMNS = (
    ("resume_mode", "VARCHAR NOT NULL DEFAULT 'normalized_replay'"),
    ("remote_session_id", "VARCHAR"),
    ("remote_interaction_id", "VARCHAR"),
    # Kept separate from remote_interaction_id rather than folded into it. Gemini's
    # `previous_interaction_id` and the Responses family's `previous_response_id` are different
    # fields with different lifetimes on different endpoints, and a provider can expose both.
    # One column would force a resume to guess which field a stored id belongs in, and guessing
    # wrong sends a valid-looking id the provider will reject.
    ("remote_response_id", "VARCHAR"),
    ("continuation_path", "VARCHAR"),
    ("continuation_hash", "VARCHAR"),
    ("continuation_schema", "INTEGER"),
    ("runtime_version", "VARCHAR"),
    ("provider_fingerprint", "VARCHAR"),
    ("model_fingerprint", "VARCHAR"),
    ("project_fingerprint", "VARCHAR"),
)

_KNOWN_RESUME_MODES = frozenset(
    {
        "native_session",
        "server_interaction",
        "client_native_replay",
        "normalized_replay",
        "unsupported",
    }
)


def _m0017_session_resume(conn: Connection) -> None:
    """Record how each session can be resumed, and what to check first (spec §23.3).

    Every column here is something :func:`~openagent.services.resume.verify_resume` compares. Storing
    the continuation's *path and hash* rather than its bytes keeps the row small enough to list
    sessions cheaply while still making a truncated artifact detectable — and a truncated native
    message is the failure that does not error, it just degrades the turn.

    The four fingerprints exist because each one changes independently and each one changes what a
    resume means: the project decides whether a CLI session id is even addressable, the provider
    fingerprint decides whether server-held state is still visible, the model fingerprint decides
    whether recorded capabilities still apply, and the runtime version decides whether the session
    format is the same one.
    """

    if not _table_exists(conn, "runs"):
        return

    for column, ddl in _RESUME_COLUMNS:
        _add_column(conn, "runs", column, ddl)

    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_runs_remote_session ON runs (remote_session_id)"
    )

    # Backfill from what each run already recorded. A run that carried a session id was resumable by
    # that id all along; writing the mode down makes the resume path explicit instead of re-derived.
    #
    # The column this reads is `provider_session_id`. The two names tried before it — `session_id`
    # and `cli_session_id` — have never existed on this table, so the backfill matched nothing and
    # every run was left `normalized_replay` with an empty remote_session_id. Measured on a real
    # database: 19 of 33 runs carried a session id and none of them were migrated, which silently
    # downgraded 19 resumable sessions to replay-only.
    session_column = next(
        (
            candidate
            for candidate in ("provider_session_id", "session_id", "cli_session_id")
            if _column_exists(conn, "runs", candidate)
        ),
        None,
    )
    if session_column is not None:
        # A session id does not by itself say *which kind* of session it is. A CLI run's id names a
        # session the CLI owns and can reattach to; an API run's names provider-side state that only
        # means anything if that provider was actually asked to keep it. Treating every id as a CLI
        # native session would promise reattachment for runs that have nothing to reattach to, so
        # the mode comes from the agent's runtime and the API case stays conservative.
        conn.exec_driver_sql(
            "UPDATE runs SET resume_mode='native_session', remote_session_id="
            f"{session_column} "
            f"WHERE {session_column} IS NOT NULL AND {session_column} != '' "
            "AND agent IN (SELECT name FROM agents WHERE runtime_type='cli')"
        )
        # A run whose agent no longer exists matches neither statement and keeps
        # `normalized_replay`. That is the right answer, not an oversight: with the agent gone
        # there is nothing to say whether the id names a CLI session or provider-side state, and
        # guessing either way would promise a resume that cannot be honoured.
        #
        # API runs keep their id, but as a *remote interaction* reference and without claiming a
        # resumable mode: whether the provider retained anything depends on server_state_enabled,
        # which this migration cannot verify retroactively.
        conn.exec_driver_sql(
            "UPDATE runs SET remote_interaction_id="
            f"{session_column} "
            f"WHERE {session_column} IS NOT NULL AND {session_column} != '' "
            "AND agent IN (SELECT name FROM agents WHERE runtime_type='api-agent')"
        )

    invalid = [
        str(row[0])
        for row in conn.exec_driver_sql(
            "SELECT id FROM runs WHERE resume_mode NOT IN "
            "('native_session','server_interaction','client_native_replay','normalized_replay',"
            "'unsupported')"
        )
    ]
    if invalid:
        listed = ", ".join(_redacted_record_id(value) for value in sorted(invalid))
        raise MigrationVerificationError(
            f"these runs carry a resume mode this build does not implement: {listed}"
        )


# --------------------------------------------------------------------------- chain fragment


#: The three revisions, in order, chained to :data:`BASE_REVISION`.
V2_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        "0015",
        BASE_REVISION,
        "provider protocol, discovery, region and locality",
        _m0015_provider_protocol_and_region,
        _FORWARD,
    ),
    Migration(
        "0016",
        "0015",
        "capability evidence with provenance",
        _m0016_capability_evidence,
        _FORWARD,
    ),
    Migration(
        "0017",
        "0016",
        "session resume mode, continuation reference and fingerprints",
        _m0017_session_resume,
        _FORWARD,
    ),
)


def base_revision_present(migrations: list[Migration] | None = None) -> bool:
    """Whether the revision this fragment chains onto is in the active chain."""

    chain = migrations if migrations is not None else MIGRATIONS
    return any(migration.revision == BASE_REVISION for migration in chain)


def register_v2_migrations(migrations: list[Migration] | None = None) -> bool:
    """Splice 0015–0017 onto the chain, if and only if 0014 is there.

    Returns whether they were registered. Called at import time by nothing: the caller is the module
    that owns the chain, and on this branch that call has not been made because 0014 is still on the
    release branches. Splicing regardless would leave a hole at 0014 that
    :func:`~.migrations.run_migrations` walks straight into, on a user's database.
    """

    chain = migrations if migrations is not None else MIGRATIONS
    if not base_revision_present(chain):
        return False
    existing = {migration.revision for migration in chain}
    for migration in V2_MIGRATIONS:
        if migration.revision not in existing:
            chain.append(migration)
    return True


def registration_status() -> dict[str, Any]:
    """A Doctor-readable explanation of why these are or are not active."""

    present = base_revision_present()
    return {
        "revisions": [migration.revision for migration in V2_MIGRATIONS],
        "base_revision": BASE_REVISION,
        "base_revision_present": present,
        "registered": present
        and all(
            migration.revision in {m.revision for m in MIGRATIONS} for migration in V2_MIGRATIONS
        ),
        "reason": (
            "active"
            if present
            else (
                f"revision {BASE_REVISION} is not in this build's migration chain; 0015-0017 chain "
                f"onto it and are held back until it lands, because a chain with a hole would be "
                f"walked into on a user's database"
            )
        ),
    }


# --------------------------------------------------------------------------- helpers


def _payload(raw: object) -> dict[str, Any]:
    """Parse a domain JSON blob, tolerating the ways it can be stored."""

    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
