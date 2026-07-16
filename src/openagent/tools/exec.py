"""Command-execution tools (spec §2.1, §27, §29).

Every command is screened by the command policy, then run inside the workspace with:

* a **minimal environment** — never the parent process's environment, so provider keys, GitHub
  tokens, AWS keys, ``DATABASE_URL`` and other secrets can't leak into a child (spec §7);
* ``shell=False`` and a structured argv by default — the executable allowlist is the boundary;
* a bounded timeout that terminates the whole process tree on expiry;
* truncated output to keep events small.

Shell-operator commands and off-allowlist executables are a *separate, higher-risk* path that only
runs after an explicit approval (the policy returns ``APPROVAL``).
"""

from __future__ import annotations

import subprocess

from ..security.command_policy import Decision, Purpose, evaluate
from ..security.process import OutputLimitExceeded, minimal_environment, run_capture
from .base import ToolContext, ToolError, ToolResult

#: A hard **byte** cap on a single command's combined stdout+stderr (item 9.3). Enforced inside
#: ``run_capture`` as the process runs — nothing beyond it is ever buffered — not sliced afterwards.
_MAX_OUTPUT_BYTES = 20_000
_DEFAULT_TIMEOUT = 300


def _run(
    ctx: ToolContext, command: str, timeout: int, purpose: Purpose = Purpose.COMMAND
) -> subprocess.CompletedProcess[str]:
    # The policy needs the profile AND the workspace to decide: without them it cannot tell an
    # unattended-safe inspection from an interpreter, or a workspace path from /etc/passwd (spec §2).
    policy = evaluate(
        command,
        network_allowed=ctx.profile.network_allowed,
        workspace_root=ctx.workspace_root,
        profile=ctx.profile,
        purpose=purpose,
    )
    if policy.decision is Decision.DENY:
        raise ToolError(f"command denied by policy: {policy.reason}")
    if policy.decision is Decision.APPROVAL:
        detail = f"{command}\n({policy.reason})"
        if not ctx.request_approval("run_command", detail, command=command, reason=policy.reason):
            raise ToolError(f"command not approved: {policy.reason}")
    if ctx.emit:
        ctx.emit("command.started", {"command": command, "cwd": str(ctx.workspace_root)})

    # Minimal environment only: the parent env (and any secrets in it) is never inherited. A run may
    # inject specific credentials via ``ctx.command_env`` for one operation; nothing else leaks.
    env = minimal_environment(ctx.command_env)
    # ``shell=False`` with the screened argv unless approval was granted for a shell-operator command.
    argv: list[str] | str = command if policy.needs_shell else list(policy.argv)
    try:
        return run_capture(
            argv,
            cwd=ctx.workspace_root,
            env=env,
            timeout=timeout,
            shell=policy.needs_shell,
            max_output_bytes=_MAX_OUTPUT_BYTES,
            cancellation=ctx.cancellation,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"command timed out after {timeout}s") from exc
    except OutputLimitExceeded as exc:
        # Never echo the runaway output back in the error — that would defeat the memory bound.
        raise ToolError(f"command output exceeded {_MAX_OUTPUT_BYTES} bytes") from exc
    except FileNotFoundError as exc:
        raise ToolError(f"executable not found: {exc}") from exc


def run_command(ctx: ToolContext, command: str, timeout: int = _DEFAULT_TIMEOUT) -> ToolResult:
    if not ctx.profile.can_run_commands:
        raise ToolError("this permission profile does not allow running commands")
    proc = _run(ctx, command, timeout)
    output = ((proc.stdout or "") + (proc.stderr or ""))[:_MAX_OUTPUT_BYTES]
    if ctx.emit:
        ctx.emit("command.completed", {"command": command, "exit_code": proc.returncode})
    ok = proc.returncode == 0
    return ToolResult(
        ok=ok, content=output, data={"exit_code": proc.returncode, "command": command}
    )


def run_tests(
    ctx: ToolContext, command: str = "pytest -q", timeout: int = _DEFAULT_TIMEOUT
) -> ToolResult:
    if not ctx.profile.can_run_commands:
        raise ToolError("this permission profile does not allow running commands")
    # Purpose.TEST opens the structured test/build runner list (spec §2.3) — and only that list.
    # "Run the project's tests" genuinely executes project code, so it is an explicit, named
    # capability rather than something a generic run_command can reach unattended.
    proc = _run(ctx, command, timeout, purpose=Purpose.TEST)
    output = ((proc.stdout or "") + (proc.stderr or ""))[:_MAX_OUTPUT_BYTES]
    passed = proc.returncode == 0
    if ctx.emit:
        ctx.emit(
            "test.completed", {"command": command, "passed": passed, "exit_code": proc.returncode}
        )
    return ToolResult(
        ok=passed,
        content=output,
        data={"exit_code": proc.returncode, "passed": passed, "command": command},
    )
