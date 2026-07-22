"""Console entrypoint.

Bare ``openagent`` opens the TUI (spec §1); any subcommand routes to the Typer CLI (spec §32).
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    argv = sys.argv[1:]
    try:
        if not argv:
            from .tui.app import run_tui

            run_tui()
            return
        from .cli.app import app

        app()
    except Exception as exc:
        from .core.errors import DatabaseReaderCompatibilityError, DataValidationError
        from .storage.migrations import (
            MigrationFailedError,
            MigrationVerificationError,
            SchemaTooNewError,
            UnknownRevisionError,
        )

        # Expected operational failures are rendered as a clean, redacted, actionable message with a
        # stable exit code and a Doctor JSON contract — never a raw traceback (spec §6.3, §7.3,
        # §17). A too-old binary reading a newer DB, a corrupt record, a blocked migration all land
        # here whether the TUI or a CLI command triggered them. OPENAGENT_DEBUG re-raises for devs.
        if _debug_enabled():
            raise
        if isinstance(exc, MigrationFailedError):
            _database_startup_failure(
                exc, argv, exit_code=3, kind="migration_failed", backup_path=exc.backup_path
            )
        if isinstance(exc, DatabaseReaderCompatibilityError):
            _database_startup_failure(exc, argv, exit_code=2, kind="database_incompatible")
        if isinstance(exc, DataValidationError):
            _database_startup_failure(exc, argv, exit_code=2, kind="data_validation")
        if isinstance(exc, (MigrationVerificationError, SchemaTooNewError, UnknownRevisionError)):
            _database_startup_failure(exc, argv, exit_code=2, kind="database_incompatible")
        raise


def _debug_enabled() -> bool:
    import os

    return os.environ.get("OPENAGENT_DEBUG", "").strip().lower() in {"1", "true", "yes"}


_KIND_CHECK_NAME = {
    "migration_failed": "Database migration",
    "database_incompatible": "Database compatibility",
    "data_validation": "Database record",
}


def _database_startup_failure(
    exc: Exception,
    argv: list[str],
    *,
    exit_code: int,
    kind: str,
    backup_path: object | None = None,
) -> None:
    """Render database startup failures without a traceback, including Doctor's JSON contract."""

    from .core.errors import redact_secrets

    detail = redact_secrets(str(exc))
    if argv and argv[0] == "doctor" and "--json" in argv:
        payload = {
            "checks": [
                {
                    "name": _KIND_CHECK_NAME.get(kind, "Database"),
                    "status": "fail",
                    "detail": detail,
                    "data": {
                        "error_type": kind,
                        "backup_path": str(backup_path) if backup_path is not None else None,
                    },
                    "exit_code_hint": exit_code,
                }
            ],
            "exit_code": exit_code,
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stderr.write(f"openagent: {kind}: {detail}\n")
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
