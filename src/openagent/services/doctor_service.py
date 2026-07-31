"""System diagnostics (spec §41).

``openagent doctor`` runs local, offline checks: config/DB/keychain/git health, which CLIs are
installed and whether they look authenticated, configured providers, and OPENAGENT.md sync. Live
provider network tests are intentionally excluded so doctor stays fast and CI-safe.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from ..core.models import CliUpdateState, CredentialType, RuntimeType
from ..credentials.store import keychain_available
from ..providers.factory import get_preset, is_nvidia_build_endpoint
from ..reporting.openagent_md import (
    OpenAgentMdConflict,
    plan_openagent_md,
    render_agents_block,
)
from ..runtimes.cli.registry import (
    cli_install_status,
    cli_registry_entries,
    discover_cli_models,
    known_cli_types,
)
from ..storage.event_log import EventLog
from ..workspaces.worktree import is_git_repo

if TYPE_CHECKING:
    from ..app import OpenAgentApp

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    data: dict = field(default_factory=dict)
    exit_code_hint: int | None = None

    def to_dict(self) -> dict:
        result: dict[str, object] = {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "data": self.data,
        }
        if self.exit_code_hint is not None:
            result["exit_code_hint"] = self.exit_code_hint
        return result


class DoctorService:
    def __init__(self, app: OpenAgentApp) -> None:
        self.app = app

    async def run(self, *, refresh_cli_updates: bool = False) -> list[Check]:
        checks: list[Check] = []
        checks.append(Check("OpenAgent configuration", OK, str(self.app.paths.config_dir)))
        checks.append(
            Check(
                "SQLite writable",
                OK if self.app.db.writable() else FAIL,
                str(self.app.paths.db_path),
                exit_code_hint=2 if not self.app.db.writable() else None,
            )
        )
        migration = self.app.db.migration_report
        if migration is not None:
            checks.append(
                Check(
                    "Database migration",
                    OK,
                    (
                        f"schema {migration.to_revision}; applied "
                        f"{', '.join(migration.applied) if migration.applied else 'none'}"
                    ),
                    data={
                        "from_revision": migration.from_revision,
                        "to_revision": migration.to_revision,
                        "applied": list(migration.applied),
                        "backup_path": (
                            str(migration.backup_path)
                            if migration.backup_path is not None
                            else None
                        ),
                        "integrity_check": migration.integrity_check,
                    },
                )
            )
        checks.append(
            Check(
                "OS keychain available",
                OK if keychain_available() else WARN,
                "keychain backend present"
                if keychain_available()
                else "no usable keychain backend",
            )
        )
        git = shutil.which("git")
        checks.append(Check("Git installed", OK if git else FAIL, git or "git not found"))
        is_repo = is_git_repo(self.app.paths.project_root)
        checks.append(
            Check(
                "Current directory is a Git repository",
                OK if is_repo else WARN,
                "worktree isolation available"
                if is_repo
                else "non-git: runs use lower-safety copies",
            )
        )

        try:
            checks.extend(await self._cli_checks(refresh_updates=refresh_cli_updates))
        except Exception:  # noqa: BLE001 - Doctor must remain available for corrupt domain rows
            checks.append(self._unavailable_domain_check("CLI installation records"))
        for name, loader in (
            ("Provider records", self._provider_checks),
            ("Agent records", self._agent_checks),
        ):
            try:
                checks.extend(loader())
            except Exception:  # noqa: BLE001 - report classification, never record contents
                checks.append(self._unavailable_domain_check(name))
        try:
            checks.append(self._event_store_check())
        except Exception:  # noqa: BLE001
            checks.append(self._unavailable_domain_check("Event records"))
        try:
            checks.append(self._openagent_md_check())
        except Exception:  # noqa: BLE001
            checks.append(Check("OPENAGENT.md sync", WARN, "could not inspect project document"))
        try:
            checks.append(self._journal_check())
        except Exception:  # noqa: BLE001
            checks.append(Check("Operation journal", WARN, "could not inspect the journal"))
        try:
            checks.append(self._provider_agent_integrity_check())
        except Exception:  # noqa: BLE001
            checks.append(self._unavailable_domain_check("Provider/agent integrity"))
        return checks

    @staticmethod
    def _unavailable_domain_check(name: str) -> Check:
        return Check(
            name,
            FAIL,
            "domain records are unavailable or incompatible; inspect the migration backup and "
            "database health before continuing",
            exit_code_hint=2,
        )

    async def _cli_checks(self, *, refresh_updates: bool = False) -> list[Check]:
        """Per-CLI readiness for every known runtime — Codex, Claude Code, and Antigravity (item 18).

        For each installed CLI, distinguishes: executable detected, authentication detected,
        structured-output/resume support, and the adapter's honest verification status (spec §17).
        A binary being present is never reported as "ready" on its own.
        """

        if refresh_updates:
            await self.app.clis.check_updates(refresh=True)
        else:
            # Local discovery refreshes paths/versions while retaining a matching cached update
            # status. It never performs a network request.
            await self.app.clis.discover(persist=True)
        persisted = {installation.type: installation for installation in self.app.clis.list()}

        checks: list[Check] = []
        for entry in await cli_registry_entries():
            name = entry.display_name
            stored = persisted.get(entry.type)
            if not entry.installed:
                checks.append(Check(f"{name} executable", WARN, "not found"))
                continue
            checks.append(
                Check(
                    f"{name} executable",
                    OK,
                    entry.executable or "detected",
                    data={
                        "active_executable": entry.executable,
                        "resolved_executable": (
                            stored.resolved_executable if stored else entry.resolved_executable
                        ),
                    },
                )
            )
            checks.append(
                Check(
                    f"{name} version",
                    OK if entry.version else WARN,
                    entry.version or "could not determine version",
                )
            )
            source = stored.install_source.value if stored else entry.install_source
            checks.append(
                Check(
                    f"{name} install source",
                    OK if source != "unknown" else WARN,
                    source,
                    data={
                        "install_source": source,
                        "release_channel": stored.release_channel
                        if stored
                        else entry.release_channel,
                        "minimum_version": stored.minimum_version if stored else None,
                        "auto_updates_disabled": (
                            stored.auto_updates_disabled if stored else False
                        ),
                        "package_manager_auto_update": (
                            stored.package_manager_auto_update if stored else None
                        ),
                    },
                )
            )
            shadowed = (
                list(stored.shadowed_executables)
                if stored is not None
                else list(entry.shadowed_executables)
            )
            conflict_detail = (
                ", ".join(shadowed)
                if shadowed
                else ("desktop executable conflict detected" if entry.desktop_conflict else "none")
            )
            checks.append(
                Check(
                    f"{name} conflicting installations",
                    WARN if shadowed or entry.desktop_conflict else OK,
                    conflict_detail,
                    data={
                        "shadowed_executables": shadowed,
                        "path_conflict": bool(shadowed),
                        "desktop_executable_conflict": entry.desktop_conflict,
                    },
                )
            )
            checks.append(
                Check(
                    f"{name} authentication",
                    OK if entry.authenticated else WARN,
                    entry.auth_detail
                    or (
                        "authenticated"
                        if entry.authenticated is True
                        else "unauthenticated"
                        if entry.authenticated is False
                        else "unknown"
                    ),
                )
            )
            caps = (
                f"structured output: {'yes' if entry.structured_events else 'no'}, "
                f"resume: {'yes' if entry.resumable else 'no'}"
                f"{' (experimental)' if entry.experimental else ''}"
            )
            # An adapter validated against one version cannot claim "verified" on another (item 16).
            verified = entry.version_verified or not entry.validated_version
            checks.append(
                Check(
                    f"{name} adapter status",
                    OK if verified else WARN,
                    f"{entry.status_label}; {caps}",
                )
            )
            update = stored.update_status if stored is not None else None
            if update is None:
                update_status = WARN
                update_detail = "not checked (cached/offline); use --refresh-cli-updates"
                update_data: dict = {
                    "current_version": entry.version,
                    "latest_version": None,
                    "install_source": source,
                    "active_executable": entry.executable,
                    "shadowed_executables": shadowed,
                    "checked_at": None,
                    "update_available": None,
                }
            else:
                update_status = (
                    OK if update.state in {CliUpdateState.CURRENT, CliUpdateState.UNKNOWN} else WARN
                )
                update_detail = update.detail or update.state.value
                update_data = update.model_dump(mode="json")
            checks.append(
                Check(
                    f"{name} update",
                    update_status,
                    update_detail,
                    data=update_data,
                )
            )
            checks.append(await self._model_discovery_check(name, entry))
            active_runs = self.app.clis.active_run_ids(entry.type)
            checks.append(
                Check(
                    f"{name} active-run/update safety",
                    WARN if active_runs else OK,
                    (
                        f"update blocked while active: {', '.join(active_runs[:5])}"
                        if active_runs
                        else "no active runs use this CLI"
                    ),
                    data={"active_run_ids": active_runs},
                )
            )
            if entry.type == "antigravity":
                if stored is not None:
                    checks.append(
                        Check(
                            "Antigravity updater lock",
                            WARN if stored.updater_lock_present else OK,
                            (
                                f"present at {stored.updater_lock_path}; "
                                "OpenAgent will not remove it"
                                if stored.updater_lock_present
                                else f"not present ({stored.updater_lock_path})"
                            ),
                            data={
                                "path": stored.updater_lock_path,
                                "present": stored.updater_lock_present,
                            },
                        )
                    )
                checks.append(self._antigravity_permission_check())
        return checks

    async def _model_discovery_check(self, name: str, entry) -> Check:
        """Report what discovery actually returned, not merely that a method name exists.

        The previous check was ``OK if entry.model_discovery_method else WARN``. That attribute is
        a **static class attribute** on each adapter, so it is always a non-empty string — the
        check reported OK unconditionally and never ran discovery at all. A CLI whose model listing
        was broken, unauthorised, or returning nothing looked healthy in ``openagent doctor``,
        which is precisely the command a user runs to find out why it is not.

        Discovery is run for the *current project*, since a repository's ``.claude/settings.json``
        can change the answer, and reported with the fields that let a user act: how the list was
        obtained, how many models it holds, whether it is partial, and the real error if any.
        """

        try:
            result = await discover_cli_models(
                entry.type,
                entry.executable,
                project_root=self.app.paths.project_root,
            )
        except Exception as exc:  # noqa: BLE001 - a broken probe is a diagnostic, not a crash
            return Check(
                f"{name} model discovery",
                WARN,
                f"discovery raised {type(exc).__name__}: {str(exc)[:200]}",
                data={"available": False, "error": str(exc)[:500]},
            )

        data = {
            "available": result.available,
            "method": result.method,
            "model_count": len(result.models),
            "partial": result.partial,
            "error": result.error or None,
            "project_root": str(self.app.paths.project_root),
        }

        if not result.available:
            # Not a failure: several CLIs legitimately have no offline model listing, and a manual
            # model id or the CLI's own default is a supported configuration.
            return Check(
                f"{name} model discovery",
                WARN,
                result.error or "no model listing; use a manual model id or the CLI default",
                data=data,
            )
        if result.partial:
            return Check(
                f"{name} model discovery",
                WARN,
                f"{len(result.models)} model(s) via {result.method}; "
                f"incomplete: {result.error or 'some sources did not answer'}",
                data=data,
            )
        if not result.models:
            return Check(
                f"{name} model discovery",
                WARN,
                f"{result.method} returned no models",
                data=data,
            )
        return Check(
            f"{name} model discovery",
            OK,
            f"{len(result.models)} model(s) via {result.method}",
            data=data,
        )

    @staticmethod
    def exit_code(checks: list[Check]) -> int:
        """Map diagnostics onto the documented stable doctor exit-code contract."""

        hinted = [check.exit_code_hint for check in checks if check.exit_code_hint is not None]
        if hinted:
            return max(hinted)
        if any(check.status != OK for check in checks):
            return 1
        return 0

    def _antigravity_permission_check(self) -> Check:
        """What Antigravity is actually allowed to do right now, and why (item 15)."""

        from .preflight import antigravity_permission_status

        edit_ok, reason = antigravity_permission_status("safe-edit")
        if not edit_ok:
            return Check(
                "antigravity permissions",
                OK,
                "read-only (supported). Editing is experimental and OFF: a non-interactive "
                "--print run can only edit with --dangerously-skip-permissions, which disables "
                "Antigravity's own tool checks. Set OPENAGENT_ANTIGRAVITY_EXPERIMENTAL_EDIT=1 to "
                "opt in.",
            )
        return Check(
            "antigravity permissions",
            WARN,
            f"editing ENABLED — Antigravity's native permission checks are bypassed ({reason})",
        )

    def _provider_checks(self) -> list[Check]:
        # decode_report never raises on a corrupt row (unlike list()), so doctor can survey and
        # report an undecodable provider instead of dying with the ValidationError it exists to
        # diagnose (spec §7.3).
        providers, decode_errors = self.app.repos.providers.decode_report()
        checks: list[Check] = []
        for err in decode_errors:
            record_id = str(err["record_id"])
            checks.append(
                Check(
                    f"Provider record: {record_id}",
                    FAIL,
                    f"record {record_id!r} could not be decoded into the current provider model; "
                    "no data was changed. Repair by reinstalling the current OpenAgent: "
                    "openagent update --repair",
                    data={
                        "table": err["table"],
                        "record_id": record_id,
                        "error_category": "data_validation",
                        "error_count": err["error_count"],
                        "safe_repair": True,
                        "backup_path": None,
                    },
                    exit_code_hint=2,
                )
            )
        if not providers:
            if decode_errors:
                return checks
            return [Check("Providers configured", WARN, "no API providers added yet")]
        checks.append(Check("Providers configured", OK, ", ".join(p.name for p in providers)))
        for p in providers:
            if p.provider_type == "openai" and is_nvidia_build_endpoint(p.base_url):
                checks.append(
                    Check(
                        f"Provider mapping: {p.name}",
                        FAIL,
                        "the NVIDIA Build endpoint is still mapped as openai; migration 0010 "
                        "should map this exact legacy record to nvidia-build",
                        data={"provider_type": p.provider_type, "expected_type": "nvidia-build"},
                        exit_code_hint=2,
                    )
                )
            checks.append(self._provider_credential_check(p))
        return checks

    def _provider_credential_check(self, provider) -> Check:
        """Offline credential health for one provider (item 20) — no network call."""

        name = f"Credential: {provider.name}"
        cred = provider.credential
        preset = get_preset(provider.provider_type)
        needs_key = preset.needs_key if preset else True

        if cred.type is CredentialType.ENV:
            if not cred.env_var:
                return Check(name, FAIL, "env credential has no variable name")
            if os.environ.get(cred.env_var) is None:
                return Check(name, WARN, f"env var {cred.env_var} is not set")
            return Check(name, OK, f"env var {cred.env_var} is set")
        if cred.type is CredentialType.KEYCHAIN:
            if not self.app.credentials.available(cred):
                sev = FAIL if needs_key else WARN
                return Check(name, sev, "no stored key in the keychain")
            return Check(name, OK, "key present in keychain")
        if cred.type is CredentialType.NONE:
            if needs_key:
                return Check(name, FAIL, "no credential but this provider type requires a key")
            return Check(name, OK, "no key required")
        return Check(name, OK, cred.type.value)

    def _agent_checks(self) -> list[Check]:
        agents = self.app.repos.agents.list()
        if not agents:
            return []
        provider_names = {p.name for p in self.app.repos.providers.list()}
        installed = {c for c, ok in cli_install_status() if ok}
        known = set(known_cli_types())
        checks: list[Check] = []
        for agent in agents:
            rt = agent.runtime
            rtype = rt.type if isinstance(rt.type, str) else rt.type.value
            label = f"Agent: {agent.name}"
            if rtype == RuntimeType.API_AGENT.value:
                if rt.provider not in provider_names:
                    checks.append(
                        Check(label, FAIL, f"references missing provider {rt.provider!r}")
                    )
                else:
                    checks.append(Check(label, OK, f"provider {rt.provider!r} present"))
            else:
                cli = rt.cli or ""
                if cli not in known:
                    checks.append(Check(label, WARN, f"unknown CLI runtime {cli!r}"))
                elif cli not in installed:
                    checks.append(Check(label, WARN, f"CLI {cli!r} is not installed"))
                else:
                    checks.append(Check(label, OK, f"CLI {cli!r} installed"))
        return checks

    def _openagent_md_check(self) -> Check:
        path = self.app.paths.openagent_md()
        agents = self.app.repos.agents.list()
        if not path.exists():
            return Check(
                "OPENAGENT.md synchronized",
                WARN if agents else OK,
                "not generated yet" if agents else "no agents to document",
            )

        # A document OpenAgent refuses to regenerate is a distinct state from a stale one, and the
        # user needs to know which: "stale" is fixed by adding an agent, a conflict is not fixed by
        # anything except editing the file. Reporting the second as the first sends people in
        # circles re-running commands that decline to write.
        try:
            plan_openagent_md(path, agents)
        except OpenAgentMdConflict as conflict:
            return Check(
                "OPENAGENT.md synchronized",
                WARN,
                f"{conflict.reason}; OpenAgent will not overwrite it. "
                f"Preview the replacement with `openagent agent sync-document --dry-run`",
                data={"conflict": conflict.reason, "path": str(path)},
            )

        expected = render_agents_block(agents).strip()
        synced = expected in path.read_text(encoding="utf-8")
        return Check(
            "OPENAGENT.md synchronized",
            OK if synced else WARN,
            "up to date" if synced else "stale; re-run `openagent add`/`remove`",
        )

    @staticmethod
    def _recovery_state(kind: str, stage: str) -> str:
        """A redacted, human-readable recovery state for one pending operation.

        Distinguishes the provider-recovery outcomes a user (or an operator triaging a support
        report) needs to tell apart — a preserved-but-ambiguous ownership, a still-pending
        legacy-secret cleanup on an otherwise-committed provider, a genuine rollback, a superseded
        generation — without ever rendering the payload (which can carry a credential ref, header
        or URL).
        """

        from ..services.provider_service import (
            STAGE_COMMIT_DURABLE,
            STAGE_LEGACY_CLEANUP_PENDING,
            STAGE_OWNED_COMPENSATION,
            STAGE_RECOVERY_AMBIGUOUS,
            STAGE_ROLLBACK_PENDING,
            STAGE_SUPERSEDED_GENERATION,
        )

        if kind == "agent_document_sync":
            return "OPENAGENT.md sync pending"
        if kind in {"provider_add", "provider_remove"}:
            return {
                STAGE_ROLLBACK_PENDING: "provider rollback pending",
                STAGE_OWNED_COMPENSATION: "provider rollback pending",
                STAGE_COMMIT_DURABLE: "provider legacy credential cleanup pending",
                STAGE_LEGACY_CLEANUP_PENDING: "provider legacy credential cleanup pending",
                STAGE_RECOVERY_AMBIGUOUS: "provider recovery ownership ambiguous",
                STAGE_SUPERSEDED_GENERATION: "provider generation superseded",
            }.get(stage, "provider recovery retry pending")
        return f"{kind} pending"

    def _journal_check(self) -> Check:
        """Pending compensating operations left behind by an interrupted write.

        These are normally invisible: startup replays them. One that *stays* pending across
        restarts means the replay itself keeps failing — most often an OPENAGENT.md conflict, which
        recovery deliberately skips rather than letting it block startup, or a provider whose
        ownership recovery could not verify (which recovery deliberately preserves rather than
        risk deleting committed data). Without this check that situation is completely silent.
        """

        pending = self.app.journal.pending()
        if not pending:
            return Check("Operation journal", OK, "no interrupted operations")
        kinds: dict[str, int] = {}
        states: dict[str, int] = {}
        for operation in pending:
            kinds[operation.kind] = kinds.get(operation.kind, 0) + 1
            state = self._recovery_state(operation.kind, operation.stage)
            states[state] = states.get(state, 0) + 1
        summary = ", ".join(f"{state} x{count}" for state, count in sorted(states.items()))
        return Check(
            "Operation journal",
            WARN,
            f"{len(pending)} operation(s) still pending after recovery: {summary}",
            data={"pending": len(pending), "kinds": kinds, "states": states},
        )

    def _provider_agent_integrity_check(self) -> Check:
        """Cross-check the relational binding the v0.1.6 foreign key is supposed to guarantee.

        The constraint makes an orphaned binding unrepresentable *going forward*. This check exists
        for databases that were upgraded rather than created fresh, where a pre-0012 record could
        have been left in a shape the constraint would now reject.
        """

        from ..storage import db as tables

        problems: list[str] = []
        with self.app.db.engine.connect() as conn:
            orphans = conn.execute(
                select(tables.agents.c.name)
                .select_from(
                    tables.agents.outerjoin(
                        tables.provider_connections,
                        tables.agents.c.provider_id == tables.provider_connections.c.id,
                    )
                )
                .where(
                    tables.agents.c.provider_id.isnot(None),
                    tables.provider_connections.c.id.is_(None),
                )
            ).all()
            if orphans:
                problems.append(
                    "agents bound to a missing provider: "
                    + ", ".join(str(row[0]) for row in orphans[:5])
                )
            for table, label in (
                (tables.provider_connections, "provider"),
                (tables.agents, "agent"),
            ):
                duplicates = conn.execute(
                    select(table.c.normalized_name, func.count())
                    .group_by(table.c.normalized_name)
                    .having(func.count() > 1)
                ).all()
                if duplicates:
                    problems.append(
                        f"{label} names that differ only in case or Unicode form: "
                        + ", ".join(str(row[0]) for row in duplicates[:5])
                    )

        if problems:
            return Check(
                "Provider/agent integrity",
                FAIL,
                "; ".join(problems),
                data={"problems": problems},
                exit_code_hint=2,
            )
        return Check("Provider/agent integrity", OK, "bindings and names are consistent")

    def _event_store_check(self) -> Check:
        issues: list[str] = []
        repairable = 0
        for run in self.app.runs.list(limit=1_000):
            sequences = self.app.repos.event_index.sequences_for(run.id)
            expected = list(range(1, len(sequences) + 1))
            if sequences != expected:
                issues.append(f"{run.id}: sequence discontinuity")
            if len(sequences) != len(set(sequences)):
                issues.append(f"{run.id}: duplicate sequence")
            terminal_types = self.app.repos.event_index.terminal_types(run.id)
            status = run.status.value
            allowed_by_status: dict[str, list[tuple[str, ...]]] = {
                "completed": [("run.completed",)],
                "failed": [("run.failed",)],
                "orphaned": [("run.orphaned",)],
                "cancelled": [
                    ("run.cancelled",),
                    ("run.orphaned", "run.cancelled"),
                ],
            }
            allowed = allowed_by_status.get(status, [()])
            chain = tuple(terminal_types)
            if chain not in allowed:
                rendered = " -> ".join(terminal_types) if terminal_types else "none"
                issues.append(f"{run.id}: invalid terminal chain {rendered} for {status}")
            path = self.app.runs.run_dir_for(run) / "events.jsonl"
            try:
                exported = EventLog(path.parent).read_raw()
                exported_ids = [event.get("id") for event in exported]
            except (OSError, ValueError):
                exported_ids = []
            database_ids = [event["id"] for event in self.app.repos.event_index.read_raw(run.id)]
            if exported_ids != database_ids:
                repairable += 1
        if issues:
            return Check(
                "Event store integrity",
                FAIL,
                "; ".join(issues[:10]),
                exit_code_hint=4,
            )
        if repairable:
            return Check(
                "Event store integrity",
                WARN,
                f"SQLite bodies valid; {repairable} JSONL export(s) differ and can be repaired",
            )
        return Check("Event store integrity", OK, "continuous SQLite bodies; JSONL exports match")


def overall_ok(checks: list[Check]) -> bool:
    return all(c.status != FAIL for c in checks)
