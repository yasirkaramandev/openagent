#!/usr/bin/env python3
"""Resume an existing OpenAgent run through the application service API."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True, dest="run_id", help="existing OpenAgent run id")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--prompt", help="follow-up prompt text")
    source.add_argument("--prompt-file", type=Path, help="UTF-8 follow-up prompt file")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--all-projects", action="store_true")
    return parser


async def _resume(args: argparse.Namespace) -> dict[str, object]:
    # Import after argument parsing so ``--help`` remains a side-effect-free smoke
    # test even when OpenAgent has not yet been installed in this environment.
    from openagent.app import OpenAgentApp

    prompt = (
        args.prompt_file.read_text(encoding="utf-8")
        if args.prompt_file is not None
        else args.prompt
    )
    application = OpenAgentApp.create(args.project_root.resolve())
    result = await application.runs.resume(
        args.run_id,
        prompt,
        all_projects=args.all_projects,
    )
    status = getattr(result.status, "value", result.status)
    return {
        "run_id": result.id,
        "status": status,
        "turn": result.turns,
        "changed_files": list(result.files_changed),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(json.dumps(asyncio.run(_resume(args)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
