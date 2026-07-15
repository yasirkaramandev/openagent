"""Command tools are memory-bounded and interruptible (items 9.2, 9.3).

Two real-subprocess guarantees:

* **9.3** ``run_command``/``run_tests`` cap a single command's combined stdout+stderr at a hard byte
  limit, enforced *as the process runs* (nothing past the limit is ever buffered), and the runaway
  output never leaks back into the error message.
* **9.2** a blocking command is cancelled *mid-flight* — the whole process tree (child *and*
  grandchild) is terminated and ``RunCancelled`` reaches the caller; the tool never returns success.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from openagent.core.cancellation import RunCancellation, RunCancelled
from openagent.core.permissions import DEVELOPMENT, get_profile
from openagent.security.approvals import ApprovalGate
from openagent.tools.base import ToolContext, ToolError
from openagent.tools.exec import _MAX_OUTPUT_BYTES, run_command


def _ctx(root: Path, *, cancellation: RunCancellation | None = None) -> ToolContext:
    return ToolContext(
        workspace_root=root, profile=get_profile(DEVELOPMENT),
        approval_gate=ApprovalGate(auto_approve=True), run_id="run_x",
        cancellation=cancellation,
    )


def _py(prog: str) -> str:
    # An interpreter invocation (auto-approved in these tests) with a single-quoted body so the
    # policy's shlex split keeps it as one argv element.
    return f'{sys.executable} -c "{prog}"'


# --------------------------------------------------------------------------- 9.3 output bound


def test_output_over_the_byte_cap_raises_without_echoing_it(tmp_path: Path):
    ctx = _ctx(tmp_path)
    with pytest.raises(ToolError) as excinfo:
        run_command(ctx, _py("import sys; sys.stdout.write('A' * 100000)"))
    msg = str(excinfo.value)
    assert f"exceeded {_MAX_OUTPUT_BYTES} bytes" in msg
    assert "AAAA" not in msg, "the runaway output must not be echoed back in the error"


def test_stdout_and_stderr_share_one_combined_cap(tmp_path: Path):
    ctx = _ctx(tmp_path)
    # 15k on each stream: neither alone exceeds 20k, but combined (30k) does.
    prog = "import sys; sys.stdout.write('O' * 15000); sys.stderr.write('E' * 15000)"
    with pytest.raises(ToolError, match=f"exceeded {_MAX_OUTPUT_BYTES} bytes"):
        run_command(ctx, _py(prog))


def test_output_under_the_cap_is_returned(tmp_path: Path):
    ctx = _ctx(tmp_path)
    result = run_command(ctx, _py("import sys; sys.stdout.write('B' * 100)"))
    assert result.ok
    assert "B" * 100 in result.content


# --------------------------------------------------------------------------- 9.2 cancellation


def _alive(pid: int) -> bool:
    """Alive means a real running/sleeping process — a reaped or zombie pid does not count."""
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def test_blocking_command_cancel_kills_child_and_grandchild(tmp_path: Path):
    parent_pidfile = tmp_path / "parent.pid"
    child_pidfile = tmp_path / "child.pid"
    script = tmp_path / "spawn.py"
    # The command process (child of run_capture) spawns its own child (a grandchild of the run) and
    # then blocks; both must be dead after a cancel.
    script.write_text(
        "import os, sys, subprocess, time\n"
        "grandchild = subprocess.Popen([sys.executable, '-c',\n"
        "    \"import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(60)\",\n"
        "    sys.argv[2]])\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    cancel = RunCancellation("run_kill")
    ctx = _ctx(tmp_path, cancellation=cancel)

    def _cancel_once_spawned() -> None:
        for _ in range(200):  # up to ~10s for both processes to record their pids
            if parent_pidfile.exists() and child_pidfile.exists():
                break
            time.sleep(0.05)
        cancel.cancel("user requested stop")

    canceller = threading.Thread(target=_cancel_once_spawned)
    canceller.start()
    try:
        with pytest.raises(RunCancelled):
            run_command(ctx, f"{sys.executable} {script} {parent_pidfile} {child_pidfile}")
    finally:
        canceller.join()

    parent_pid = int(parent_pidfile.read_text())
    grandchild_pid = int(child_pidfile.read_text())
    deadline = time.time() + 5
    while time.time() < deadline and (_alive(parent_pid) or _alive(grandchild_pid)):
        time.sleep(0.05)
    assert not _alive(parent_pid), "the command process survived the cancel"
    assert not _alive(grandchild_pid), "the grandchild process survived the cancel"
