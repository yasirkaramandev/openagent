"""Process-tree management for CLI subprocesses (spec §7, §45).

Responsibilities:

* Build a **minimal environment** for children so secrets in the parent env don't leak, and inject
  only the credentials a specific run needs (spec §7).
* Launch a subprocess, expose its stdout as an async line stream, and capture stderr.
* Cancel = terminate the whole process tree (graceful ``SIGTERM`` → force ``SIGKILL``), so a
  cancelled agent never leaves orphaned children (spec §45).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

import psutil

from ..core.cancellation import RunCancellation, RunCancelled

IS_WINDOWS = sys.platform.startswith("win")

#: Environment variables that are safe/necessary to inherit for a child CLI to function.
_SAFE_ENV_KEYS = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "TMPDIR", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT",  # Windows
)


def minimal_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """A stripped-down environment: only safe keys from the parent, plus explicit ``extra``.

    Notably this does **not** carry provider API keys from the parent process (spec §7) — the caller
    injects exactly the credential a run needs via ``extra``.
    """

    env = {key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ}
    if extra:
        env.update(extra)
    return env


class ManagedProcess:
    """An async-launched subprocess whose whole tree can be cancelled cleanly."""

    def __init__(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.args = list(args)
        self.cwd = cwd
        self.env = dict(env) if env is not None else minimal_environment()
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr: list[str] = []
        self._cancelled = False
        self._create_time: float | None = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def cancelled(self) -> bool:
        """True once :meth:`cancel` has been requested (so callers can emit ``run.cancelled``)."""
        return self._cancelled

    @property
    def create_time(self) -> float | None:
        """The OS process start time — a reuse-safe identity signal for later termination."""
        return self._create_time

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self.args,
            cwd=str(self.cwd),
            env=self.env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=not IS_WINDOWS,
        )
        self._create_time = pid_identity(self._proc.pid)

    async def stream_stdout(self) -> AsyncIterator[str]:
        """Yield decoded stdout lines. Drains stderr concurrently to avoid buffer deadlock."""

        assert self._proc is not None and self._proc.stdout is not None
        stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            async for raw in self._proc.stdout:
                yield raw.decode("utf-8", errors="replace").rstrip("\n")
        finally:
            await stderr_task

    async def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        async for raw in self._proc.stderr:
            self._stderr.append(raw.decode("utf-8", errors="replace").rstrip("\n"))

    @property
    def stderr(self) -> str:
        return "\n".join(self._stderr)

    async def wait(self) -> int:
        assert self._proc is not None
        return await self._proc.wait()

    async def cancel(self, grace: float = 3.0) -> None:
        """Terminate the process and every descendant (spec §45). Idempotent."""

        self._cancelled = True
        if self._proc is None or self._proc.returncode is not None:
            return
        terminate_process_tree(self._proc.pid, grace=grace)


def _safe_signal(proc: psutil.Process, *, terminate: bool) -> None:
    try:
        if terminate:
            proc.terminate()
        else:
            proc.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):  # pragma: no cover - race/perm
        pass


def terminate_process_tree(pid: int, *, grace: float = 3.0) -> None:
    """Graceful ``SIGTERM`` → force ``SIGKILL`` of ``pid`` and every descendant (spec §45)."""

    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        return
    victims = [*children, parent]
    for proc in victims:
        _safe_signal(proc, terminate=True)
    _, alive = psutil.wait_procs(victims, timeout=grace)
    for proc in alive:  # force-kill survivors
        _safe_signal(proc, terminate=False)


def pid_identity(pid: int | None) -> float | None:
    """Return a process's start time — a signal to verify identity after possible PID reuse."""

    if not pid:
        return None
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):  # pragma: no cover - race/perm
        return None


#: How close two OS create-times must be to be considered the same process (seconds).
_IDENTITY_TOLERANCE = 1.0

#: Identity classifications for a recorded run PID (spec §45).
PID_ALIVE = "alive"      # the same process is still running (PID + create-time match)
PID_GONE = "gone"        # no such PID (or no PID recorded)
PID_REUSED = "reused"    # PID is live but belongs to a *different* process (create-time differs)
PID_UNKNOWN = "unknown"  # PID is live but identity can't be verified (no recorded time / denied)


def run_process_status(
    pid: int | None, expected_create_time: float | None, *, tolerance: float = _IDENTITY_TOLERANCE
) -> str:
    """Classify a recorded run's process by **PID + start-time identity** (spec §45).

    The shared identity check behind both orphan recovery and cross-process cancellation, so neither
    ever acts on a process that merely happens to reuse an old PID:

    * ``PID_GONE`` — no PID recorded, or no such process now;
    * ``PID_ALIVE`` — the PID is live and its create-time matches (same process);
    * ``PID_REUSED`` — the PID is live but its create-time differs (a *different* process);
    * ``PID_UNKNOWN`` — the PID is live but identity can't be verified (no recorded create-time, or
      access to the process is denied). Callers treat this fail-closed (do not touch / orphan).
    """

    if not pid:
        return PID_GONE
    try:
        actual = psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return PID_GONE
    except psutil.AccessDenied:  # pragma: no cover - perm dependent
        return PID_UNKNOWN
    if expected_create_time is None:
        return PID_UNKNOWN  # a live PID we can't tie to our run — never claim it as alive
    return PID_ALIVE if abs(actual - expected_create_time) <= tolerance else PID_REUSED


def terminate_pid_tree(
    pid: int | None, expected_create_time: float | None = None, *, grace: float = 3.0
) -> bool:
    """Terminate a run's process tree by PID, verifying identity first (spec §45).

    Guards against killing an unrelated process after PID reuse: when ``expected_create_time`` is
    given, the live process's start time must match it (via :func:`run_process_status`). Idempotent —
    returns ``False`` when the process is already gone or fails the identity check.
    """

    if not pid:
        return False
    if expected_create_time is not None:
        if run_process_status(pid, expected_create_time) != PID_ALIVE:
            return False  # gone, reused, or unverifiable — do not touch it
    else:
        try:
            psutil.Process(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
    terminate_process_tree(pid, grace=grace)
    return True


class OutputLimitExceeded(RuntimeError):
    """A command produced more output than the caller allows; its process tree was killed."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"command output exceeded {limit} bytes")
        self.limit = limit


#: The bounded reader always runs when a cancellation controller is supplied; without an explicit
#: output cap it uses this (effectively unbounded) ceiling so cancellation still works.
_UNBOUNDED = 1 << 62


def run_capture(
    argv: Sequence[str] | str,
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
    shell: bool = False,
    max_output_bytes: int | None = None,
    cancellation: RunCancellation | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command to completion in its own process group, capturing bounded text output.

    On timeout the *whole* process tree is terminated (not just the direct child) and
    :class:`subprocess.TimeoutExpired` is re-raised. ``env`` is used as-is (callers pass a minimal
    environment); the parent environment is never inherited.

    ``max_output_bytes`` is a **real** memory bound (item 9.3/18). The previous implementation called
    ``communicate()`` and only checked the size afterwards — by which point the whole output was
    already in memory, so a command emitting gigabytes would exhaust the host before the check ever
    ran. Output is now read incrementally, and the moment the combined total crosses the limit the
    process tree is killed and :class:`OutputLimitExceeded` is raised. Nothing beyond the limit is
    ever buffered.

    ``cancellation`` makes a *blocking* command interruptible (item 9.2). The reader polls the
    controller while the child runs; the instant a cancel is requested the whole process tree is
    terminated and :class:`RunCancelled` is raised — the command never gets to return a success.
    """

    popen = subprocess.Popen(  # noqa: S603 - argv is policy-screened; shell only on approval
        argv,
        cwd=str(cwd),
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=False,  # read bytes: a limit in bytes must be enforced on bytes
        shell=shell,
        start_new_session=not IS_WINDOWS,
    )
    cmd = argv if shell else list(argv)
    if max_output_bytes is None and cancellation is None:
        try:
            raw_out, raw_err = popen.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate_process_tree(popen.pid)
            try:
                popen.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                popen.kill()
            raise
        return subprocess.CompletedProcess(
            cmd, popen.returncode, _decode(raw_out), _decode(raw_err)
        )

    limit = max_output_bytes if max_output_bytes is not None else _UNBOUNDED
    out, err, exceeded = _read_bounded(
        popen, timeout=timeout, limit=limit, cancellation=cancellation
    )
    # A cancel that arrived while the command was running wins over any output it managed to produce:
    # the tree was already terminated by the reader; surface it as a cancellation, not a result.
    if cancellation is not None and cancellation.cancelled:
        raise RunCancelled(cancellation.reason or "cancelled by user")
    if exceeded and max_output_bytes is not None:
        raise OutputLimitExceeded(max_output_bytes)
    return subprocess.CompletedProcess(cmd, popen.returncode, _decode(out), _decode(err))


def _decode(raw: bytes | None) -> str:
    return (raw or b"").decode("utf-8", errors="replace")


def _read_bounded(
    popen: subprocess.Popen, *, timeout: float, limit: int,
    cancellation: RunCancellation | None = None,
) -> tuple[bytes, bytes, bool]:
    """Stream a process's stdout/stderr with a hard combined byte cap and a deadline.

    Reads both pipes on threads (portable, including Windows, where ``select`` cannot poll pipes),
    stopping the instant the cap is crossed. The process tree is terminated on breach, timeout, *or*
    a cancellation request (item 9.2), so neither a runaway producer nor a blocking command can keep
    running once we have stopped caring about its output.
    """

    import threading

    chunks: dict[str, list[bytes]] = {"out": [], "err": []}
    total = [0]
    breached = threading.Event()
    lock = threading.Lock()

    def pump(name: str, stream) -> None:
        if stream is None:
            return
        try:
            while True:
                block = stream.read(4096)
                if not block:
                    return
                with lock:
                    remaining = limit - total[0]
                    if remaining <= 0:
                        breached.set()
                        return
                    if len(block) > remaining:
                        chunks[name].append(block[:remaining])
                        total[0] = limit
                        breached.set()
                        return
                    chunks[name].append(block)
                    total[0] += len(block)
        except (OSError, ValueError):  # pragma: no cover - pipe closed under us on kill
            return
        finally:
            with contextlib.suppress(OSError):
                stream.close()

    threads = [
        threading.Thread(target=pump, args=("out", popen.stdout), daemon=True),
        threading.Thread(target=pump, args=("err", popen.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + timeout
    timed_out = False
    cancelled = False
    while True:
        if breached.is_set():
            break
        if cancellation is not None and cancellation.cancelled:
            cancelled = True
            break
        if popen.poll() is not None:
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.02)

    if breached.is_set() or timed_out or cancelled:
        terminate_process_tree(popen.pid)
    for thread in threads:
        thread.join(timeout=2.0)
    with contextlib.suppress(subprocess.TimeoutExpired):
        popen.wait(timeout=5.0)

    if timed_out and not breached.is_set():
        raise subprocess.TimeoutExpired(popen.args, timeout)
    with lock:
        return b"".join(chunks["out"]), b"".join(chunks["err"]), breached.is_set()


def is_pid_alive(pid: int | None) -> bool:
    """Whether a previously recorded run PID is still running (orphan recovery, spec §45)."""

    if not pid:
        return False
    try:
        return psutil.pid_exists(pid)
    except Exception:  # pragma: no cover - platform dependent
        return False
