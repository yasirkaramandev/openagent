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
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

import psutil

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


def terminate_pid_tree(
    pid: int | None, expected_create_time: float | None = None, *, grace: float = 3.0
) -> bool:
    """Terminate a run's process tree by PID, verifying identity first (spec §45).

    Guards against killing an unrelated process after PID reuse: if ``expected_create_time`` is
    given it must match the live process's start time (within a small tolerance). Idempotent —
    returns ``False`` when the process is already gone or fails the identity check.
    """

    if not pid:
        return False
    try:
        proc = psutil.Process(pid)
        if expected_create_time is not None:
            if abs(proc.create_time() - expected_create_time) > 1.0:
                return False  # PID was reused by a different process — do not touch it
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    terminate_process_tree(pid, grace=grace)
    return True


def run_capture(
    argv: Sequence[str] | str,
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
    shell: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command to completion in its own process group, capturing text output.

    On timeout the *whole* process tree is terminated (not just the direct child) and
    :class:`subprocess.TimeoutExpired` is re-raised. ``env`` is used as-is (callers pass a minimal
    environment); the parent environment is never inherited.
    """

    popen = subprocess.Popen(  # noqa: S603 - argv is policy-screened; shell only on approval
        argv,
        cwd=str(cwd),
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        shell=shell,
        start_new_session=not IS_WINDOWS,
    )
    try:
        out, err = popen.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_process_tree(popen.pid)
        try:
            popen.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            popen.kill()
        raise
    cmd = argv if shell else list(argv)
    return subprocess.CompletedProcess(cmd, popen.returncode, out, err)


def is_pid_alive(pid: int | None) -> bool:
    """Whether a previously recorded run PID is still running (orphan recovery, spec §45)."""

    if not pid:
        return False
    try:
        return psutil.pid_exists(pid)
    except Exception:  # pragma: no cover - platform dependent
        return False
