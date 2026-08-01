#!/usr/bin/env python3
"""Create a disposable fixed or intentionally buggy Task Board workspace.

This script performs only local file copies.  It never installs packages, reads
credentials, invokes a shell, or accesses the network.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

MARKER_NAME = ".openagent-example-copy"
GUARD_START = "            # SCENARIO_COMPLETION_GUARD_START\n"
GUARD_END = "            # SCENARIO_COMPLETION_GUARD_END\n"


class SetupError(RuntimeError):
    pass


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _replace_guard(repository: Path, fragment: Path) -> None:
    source = repository.read_text(encoding="utf-8")
    start = source.find(GUARD_START)
    end = source.find(GUARD_END, start + len(GUARD_START))
    if start < 0 or end < 0:
        raise SetupError("the controlled completion guard markers are missing")
    end += len(GUARD_END)
    replacement = fragment.read_text(encoding="utf-8")
    if not replacement.endswith("\n"):
        replacement += "\n"
    repository.write_text(source[:start] + replacement + source[end:], encoding="utf-8")


def create_workspace(
    example_root: Path, destination: Path, scenario: str, *, force: bool = False
) -> Path:
    root = example_root.resolve()
    target = destination.resolve()
    if target == root or not _inside(target, root):
        raise SetupError("destination must be a child of the example directory")
    if target.exists():
        marker = target / MARKER_NAME
        if not force:
            raise SetupError(f"destination already exists: {target}")
        if not marker.is_file():
            raise SetupError("--force only replaces a workspace created by this script")
        shutil.rmtree(target)

    target.mkdir(parents=True)
    for directory in ("app", "tests", "prompts", "expected"):
        shutil.copytree(root / directory, target / directory)
    for filename in ("README.md", "OPENAGENT.md", "pyproject.toml", ".gitignore"):
        shutil.copy2(root / filename, target / filename)

    scripts = target / "scripts"
    scripts.mkdir()
    shutil.copy2(root / "scripts" / "resume_via_api.py", scripts / "resume_via_api.py")

    if scenario == "buggy":
        _replace_guard(
            target / "app" / "repository.py",
            root / "scenarios" / "buggy" / "completion_guard.pyfrag",
        )
    elif scenario != "fixed":
        raise SetupError(f"unknown scenario: {scenario}")

    marker_payload = {"format": 1, "scenario": scenario, "source": "OpenAgent Task Board Example"}
    (target / MARKER_NAME).write_text(
        json.dumps(marker_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("buggy", "fixed"), default="buggy")
    parser.add_argument(
        "--destination",
        type=Path,
        help="child of example/ (default: example/.demo/<scenario>)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace only a prior workspace carrying this script's marker",
    )
    parser.add_argument("--json", action="store_true", dest="json_out")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    example_root = Path(__file__).resolve().parents[1]
    destination = args.destination or example_root / ".demo" / args.scenario
    if not destination.is_absolute():
        destination = example_root / destination
    try:
        created = create_workspace(example_root, destination, args.scenario, force=args.force)
    except (OSError, SetupError) as exc:
        print(f"setup failed: {exc}", file=sys.stderr)
        return 2
    payload = {"workspace": str(created), "scenario": args.scenario, "network_used": False}
    if args.json_out:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"Created {args.scenario} workspace: {created}")
        print(f"Next: cd {created} && {sys.executable} -m pytest -q")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
