"""Console entrypoint.

Bare ``openagent`` opens the TUI (spec §1); any subcommand routes to the Typer CLI (spec §32).
"""

from __future__ import annotations

import sys


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        from .tui.app import run_tui

        run_tui()
        return
    from .cli.app import app

    app()


if __name__ == "__main__":
    main()
