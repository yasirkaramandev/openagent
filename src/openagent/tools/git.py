"""Read-only git tools (spec §2.1, §4.3).

Uses the real ``git`` binary so behavior matches the user's git exactly (spec §4). These are the
tools the guarded profiles rely on *instead* of a generic ``run_command``, so every invocation goes
through one helper that pins down what "read-only git" means:

* a **minimal environment** — the parent environment (and every provider key in it) is never handed
  to a child process (spec §7). This was leaking: these tools used to inherit ``os.environ`` whole;
* ``--no-pager`` / ``--no-ext-diff`` / ``--no-textconv`` — repository configuration can point git at
  an *external program* for paging, diffing or content conversion, which turns "read a diff" into
  "run whatever the config says";
* ``GIT_TERMINAL_PROMPT=0`` and ``GIT_ASKPASS`` disabled, so git can never block the run waiting for
  credentials at a terminal nobody is watching;
* a bounded timeout and bounded output, so a pathological repository cannot hang or exhaust the run.
"""

from __future__ import annotations

import subprocess

from ..security.process import minimal_environment
from .base import ToolContext, ToolResult

#: Bounded so a huge repository cannot blow up the event log or memory.
_MAX_OUTPUT_CHARS = 40_000
_TIMEOUT_SECONDS = 60

#: Global flags that disable every config-driven hook into an external program. These come *before*
#: the subcommand because git only accepts them there.
_READ_ONLY_GLOBAL_FLAGS = ("--no-pager",)
#: Applied to commands that render diffs, where config can name an external diff/textconv program.
_READ_ONLY_DIFF_FLAGS = ("--no-ext-diff", "--no-textconv")


def _git_env() -> dict[str, str]:
    env = minimal_environment()
    # Never let git stop to ask a human for credentials in a non-interactive run.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_PAGER"] = "cat"
    return env


def _git(ctx: ToolContext, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, never a shell
        ["git", *_READ_ONLY_GLOBAL_FLAGS, *args],
        cwd=str(ctx.workspace_root),
        capture_output=True,
        text=True,
        check=False,
        timeout=_TIMEOUT_SECONDS,
        env=_git_env(),
    )


def git_status(ctx: ToolContext) -> ToolResult:
    try:
        proc = _git(ctx, ["status", "--short", "--branch"])
    except subprocess.TimeoutExpired:
        return ToolResult.failure(f"git status timed out after {_TIMEOUT_SECONDS}s")
    if proc.returncode != 0:
        return ToolResult.failure(proc.stderr.strip()[:2_000] or "not a git repository")
    return ToolResult.success(proc.stdout.strip()[:_MAX_OUTPUT_CHARS] or "(clean)")


def git_diff(ctx: ToolContext, path: str = "") -> ToolResult:
    args = ["diff", *_READ_ONLY_DIFF_FLAGS]
    if path:
        # ``--`` keeps a path that looks like an option from being read as one.
        args += ["--", path]
    try:
        proc = _git(ctx, args)
    except subprocess.TimeoutExpired:
        return ToolResult.failure(f"git diff timed out after {_TIMEOUT_SECONDS}s")
    if proc.returncode != 0:
        return ToolResult.failure(proc.stderr.strip()[:2_000] or "not a git repository")
    return ToolResult.success(proc.stdout[:_MAX_OUTPUT_CHARS])
