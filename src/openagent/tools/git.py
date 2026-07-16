"""Read-only git tools (spec §2.1).

Uses the real ``git`` binary so behavior matches the user's git exactly (spec §4). These are safe in
every profile (no mutation).
"""

from __future__ import annotations

import subprocess

from .base import ToolContext, ToolResult


def _git(ctx: ToolContext, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(ctx.workspace_root),
        capture_output=True,
        text=True,
        check=False,
    )


def git_status(ctx: ToolContext) -> ToolResult:
    proc = _git(ctx, ["status", "--short", "--branch"])
    if proc.returncode != 0:
        return ToolResult.failure(proc.stderr.strip() or "not a git repository")
    return ToolResult.success(proc.stdout.strip() or "(clean)")


def git_diff(ctx: ToolContext, path: str = "") -> ToolResult:
    args = ["diff"]
    if path:
        args += ["--", path]
    proc = _git(ctx, args)
    if proc.returncode != 0:
        return ToolResult.failure(proc.stderr.strip() or "not a git repository")
    return ToolResult.success(proc.stdout[:40_000])
