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
        from .storage.migrations import (
            MigrationFailedError,
            MigrationVerificationError,
            SchemaTooNewError,
            UnknownRevisionError,
        )

        if isinstance(exc, MigrationFailedError):
            _database_startup_failure(exc, argv, exit_code=3, backup_path=exc.backup_path)
        if isinstance(exc, (MigrationVerificationError, SchemaTooNewError, UnknownRevisionError)):
            _database_startup_failure(exc, argv, exit_code=2)
        raise


def _database_startup_failure(
    exc: Exception,
    argv: list[str],
    *,
    exit_code: int,
    backup_path: object | None = None,
) -> None:
    """Render database startup failures without a traceback, including Doctor's JSON contract."""

    kind = "migration_failed" if exit_code == 3 else "database_incompatible"
    detail = str(exc)
    if argv and argv[0] == "doctor" and "--json" in argv:
        payload = {
            "checks": [
                {
                    "name": "Database migration" if exit_code == 3 else "Database compatibility",
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
