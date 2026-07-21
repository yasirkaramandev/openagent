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
import hashlib
import os
import subprocess
import sys
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import psutil

from ..core.cancellation import RunCancellation, RunCancelled
from ..core.limits import RUNTIME_LIMITS
from ..core.models import ProcessIdentity

IS_WINDOWS = sys.platform.startswith("win")

#: How much of a child's stderr to retain, as a ring buffer keeping the **tail** (§13). The last few
#: KB is what actually diagnoses a failure; everything before it is noise a crash loop produced.
MAX_STDERR_BYTES = RUNTIME_LIMITS.cli_stderr_bytes
_STDERR_CHUNK = 8192
#: How long to let the stderr reader finish after stdout closes, before cancelling it.
_STDERR_DRAIN_GRACE = 5.0

#: The bounded window in which an async-launched child must present a stable identity (§5). A child
#: that completes inside this window is fine — there is nothing left to manage — but a child that is
#: still alive at the deadline without a readable identity is terminated fail-closed.
_STARTUP_IDENTITY_TIMEOUT = 0.25
#: How often to re-sample identity while racing the child's exit. Kept small so a fast child's exit
#: is noticed promptly, and off the event loop (``asyncio.to_thread``) so sampling never blocks it.
_IDENTITY_SAMPLE_INTERVAL = 0.01
#: How long an identity must stay unchanged before it is trusted. macOS framework Python and some
#: launcher CLIs re-exec immediately after launch, so an eager first sample records the launcher and
#: is then rejected as a mismatch. Requiring a stable window returns only the post-exec identity.
_IDENTITY_STABILITY_WINDOW = 0.05
#: Grace given to a fail-closed terminate before escalating to kill during startup.
_STARTUP_TERMINATE_GRACE = 3.0


class ProcessStartupError(RuntimeError):
    """A managed subprocess could not be brought to a manageable state at startup (§5)."""


class ProcessIdentityCaptureError(ProcessStartupError):
    """A still-live child never presented a stable identity within the startup window (§5).

    Raised only when the process is *alive* at the deadline: a child that exits before we can pin
    its identity is a legitimate fast completion, not this error.
    """


#: Environment variables that are safe/necessary to inherit for a child CLI to function.
_SAFE_ENV_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "SYSTEMROOT",
    "SystemRoot",
    "COMSPEC",
    "PATHEXT",  # Windows
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
        max_stderr_bytes: int = MAX_STDERR_BYTES,
        max_stdout_line_bytes: int = RUNTIME_LIMITS.cli_stdout_line_bytes,
        max_stdout_total_bytes: int = RUNTIME_LIMITS.cli_stdout_total_bytes,
    ) -> None:
        self.args = list(args)
        self.cwd = cwd
        self.env = dict(env) if env is not None else minimal_environment()
        self._proc: asyncio.subprocess.Process | None = None
        #: A bounded ring of the most recent stderr bytes (§13). stdout has always had a real byte
        #: cap (``run_capture``'s bounded reader); stderr was an unbounded ``list[str]``, so a backend
        #: that logs hundreds of MB — a crash loop, a progress spinner, a debug flag — was buffered
        #: in full, in RAM, for the whole run, then joined into one string on access. The tail is
        #: what diagnoses a failure, so that is what is kept.
        self._stderr_chunks: deque[bytes] = deque()
        self._stderr_bytes = 0
        self._stderr_total = 0
        self._stderr_truncated = False
        self._max_stderr_bytes = max_stderr_bytes
        self._max_stdout_line_bytes = max_stdout_line_bytes
        self._max_stdout_total_bytes = max_stdout_total_bytes
        self._stdout_total = 0
        self._stdout_limit_exceeded = False
        self._stdout_limit_detail = ""
        self._cancelled = False
        self._identity: ProcessIdentity | None = None
        #: The single lifecycle wait on the child. Created once in ``start`` and shared by identity
        #: capture, ``wait``, and startup termination, so no two call sites race their own
        #: ``proc.wait()`` — and so a startup that fails never leaves an unobserved pending task.
        self._wait_task: asyncio.Task[int] | None = None
        #: The in-flight startup identity capture. It runs asynchronously (yielding the loop), so
        #: ``pid`` can become observable to another task before ``identity`` is set. ``cancel`` awaits
        #: this so it never sees a half-started process as unkillable (§5).
        self._startup_task: asyncio.Task[ProcessIdentity | None] | None = None

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
        return self._identity.create_time if self._identity else None

    @property
    def identity(self) -> ProcessIdentity | None:
        """The full identity captured immediately after launch."""

        return self._identity

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
        # One wait task for the whole lifecycle. Identity capture races the child's exit against it,
        # rather than guessing from a single ``sleep(0)`` whether the return code has been published.
        self._wait_task = asyncio.create_task(self._proc.wait())
        # Capture runs as an awaited task, not inline, so ``cancel`` (from another task) can wait for
        # the same completion instead of racing a window where ``pid`` is set but ``identity`` is not.
        self._startup_task = asyncio.ensure_future(self._capture_startup_identity())
        self._identity = await self._startup_task

    async def _capture_startup_identity(self) -> ProcessIdentity | None:
        """Pin a stable identity, or return ``None`` for a child that has already completed (§5).

        Runs a bounded state machine that samples ``psutil`` off the event loop while watching the
        child's own wait task:

        * **child completes first** → ``None``. There is nothing left to kill; the pipes and return
          code still belong to this process, so a fast successful command loses no output.
        * **a stable identity appears** → return it (unchanged for a short window, so a re-exec's
          transient launcher identity is never the one recorded).
        * **still alive at the deadline with no identity** → terminate this exact child fail-closed
          and raise :class:`ProcessIdentityCaptureError`; never leave a live, unmanageable process.
        """

        assert self._proc is not None and self._wait_task is not None
        proc = self._proc
        wait_task = self._wait_task
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _STARTUP_IDENTITY_TIMEOUT
        last_identity: ProcessIdentity | None = None
        stable_since: float | None = None

        while loop.time() < deadline:
            if wait_task.done():
                return None
            # Sampling is synchronous psutil work; keep it off the event loop so a slow /proc read
            # never stalls every other run. Re-check the exit both sides of the sample so a PID that
            # was reused after our child exited can never be mistaken for the child.
            identity = await asyncio.to_thread(_capture_process_identity_once, proc.pid)
            if wait_task.done():
                return None
            if identity is None:
                await asyncio.sleep(_IDENTITY_SAMPLE_INTERVAL)
                continue
            if identity != last_identity:
                last_identity = identity
                stable_since = loop.time()
            elif (
                stable_since is not None
                and loop.time() - stable_since >= _IDENTITY_STABILITY_WINDOW
            ):
                return identity
            await asyncio.sleep(_IDENTITY_SAMPLE_INTERVAL)

        if wait_task.done():
            return None
        await self._terminate_unidentified()
        raise ProcessIdentityCaptureError(
            "could not capture backend process identity within the startup window"
        )

    async def _terminate_unidentified(self) -> None:
        """Terminate a still-live child we could not identify, then fully reap it (§5)."""

        assert self._proc is not None and self._wait_task is not None
        proc = self._proc
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._wait_task), timeout=_STARTUP_TERMINATE_GRACE
                )
            except (TimeoutError, asyncio.TimeoutError):
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        # Observe the wait task to completion so startup never leaves a pending, unawaited task.
        with contextlib.suppress(Exception):
            await self._wait_task

    async def stream_stdout(self) -> AsyncIterator[str]:
        """Yield bounded decoded stdout lines while draining stderr concurrently.

        ``StreamReader``'s line iterator has its own small implementation limit and raises on a
        newline-free payload. Reading chunks here lets OpenAgent enforce its documented 1 MiB/line
        and 16 MiB/stream byte limits itself. Exceeding either limit terminates the backend and is
        exposed to the adapter as ``output_limit_exceeded``; it is never a silent truncation.
        """

        assert self._proc is not None and self._proc.stdout is not None
        stderr_task = asyncio.create_task(self._drain_stderr())
        pending = bytearray()
        try:
            while True:
                raw = await self._proc.stdout.read(64 * 1024)
                if not raw:
                    break
                self._stdout_total += len(raw)
                if self._stdout_total > self._max_stdout_total_bytes:
                    self._mark_stdout_limit(
                        f"stdout exceeded {self._max_stdout_total_bytes} total bytes"
                    )
                    break
                pending.extend(raw)
                while True:
                    newline = pending.find(b"\n")
                    if newline < 0:
                        break
                    if newline > self._max_stdout_line_bytes:
                        self._mark_stdout_limit(
                            f"stdout line exceeded {self._max_stdout_line_bytes} bytes"
                        )
                        break
                    line = bytes(pending[:newline])
                    del pending[: newline + 1]
                    yield line.decode("utf-8", errors="replace")
                if self._stdout_limit_exceeded:
                    break
                if len(pending) > self._max_stdout_line_bytes:
                    self._mark_stdout_limit(
                        f"stdout line exceeded {self._max_stdout_line_bytes} bytes"
                    )
                    break
            if not self._stdout_limit_exceeded and pending:
                yield bytes(pending).decode("utf-8", errors="replace")
        finally:
            if self._stdout_limit_exceeded:
                self._terminate_for_output_limit()
            # stdout is exhausted (the child exited or was killed). Give the stderr reader a bounded
            # moment to finish draining, then stop it: a cancelled run must not leave a reader task
            # attached to a dead pipe (§13).
            if not stderr_task.done():
                try:
                    await asyncio.wait_for(stderr_task, timeout=_STDERR_DRAIN_GRACE)
                except (TimeoutError, asyncio.TimeoutError):
                    stderr_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stderr_task
            else:
                await stderr_task

    def _mark_stdout_limit(self, detail: str) -> None:
        self._stdout_limit_exceeded = True
        self._stdout_limit_detail = detail

    def _terminate_for_output_limit(self) -> None:
        if self._proc is None or self._proc.returncode is not None:
            return
        if self._identity is not None:
            terminate_process_tree(self._identity, grace=0.25)
            return
        with contextlib.suppress(ProcessLookupError):
            self._proc.kill()

    @property
    def stdout_total_bytes(self) -> int:
        return self._stdout_total

    @property
    def stdout_limit_exceeded(self) -> bool:
        return self._stdout_limit_exceeded

    @property
    def stdout_limit_detail(self) -> str:
        return self._stdout_limit_detail

    async def _drain_stderr(self) -> None:
        """Read stderr in fixed-size chunks into the bounded ring.

        Chunks, not lines: ``async for raw in stream`` is line-based, and asyncio's StreamReader
        raises ``LimitOverrunError`` once a single line exceeds its 64 KiB buffer — so one enormous
        line (no newline) both defeated the bound and could break the reader.
        """

        if self._proc is None or self._proc.stderr is None:
            return
        stream = self._proc.stderr
        while True:
            chunk = await stream.read(_STDERR_CHUNK)
            if not chunk:
                return
            self._absorb_stderr(chunk)

    def _absorb_stderr(self, chunk: bytes) -> None:
        self._stderr_total += len(chunk)
        if len(chunk) >= self._max_stderr_bytes:
            # A single chunk bigger than the whole budget: keep only its tail.
            self._stderr_chunks.clear()
            self._stderr_bytes = 0
            chunk = chunk[-self._max_stderr_bytes :]
            self._stderr_truncated = True
        self._stderr_chunks.append(chunk)
        self._stderr_bytes += len(chunk)
        while self._stderr_bytes > self._max_stderr_bytes and len(self._stderr_chunks) > 1:
            dropped = self._stderr_chunks.popleft()
            self._stderr_bytes -= len(dropped)
            self._stderr_truncated = True

    @property
    def stderr_total_bytes(self) -> int:
        """Everything the child wrote to stderr, including what the ring dropped."""

        return self._stderr_total

    @property
    def stderr_truncated(self) -> bool:
        return self._stderr_truncated

    @property
    def stderr(self) -> str:
        text = b"".join(self._stderr_chunks).decode("utf-8", errors="replace")
        if not self._stderr_truncated:
            return text
        # Say plainly that this is a tail, and how much was produced — silent truncation would make
        # a diagnosis look complete when it is not.
        return (
            f"[stderr truncated — kept the last {self._stderr_bytes} bytes of "
            f"{self._stderr_total} produced]\n{text}"
        )

    async def wait(self) -> int:
        assert self._proc is not None
        # Await the single lifecycle wait task rather than starting a second ``proc.wait()``; both
        # resolve to the same return code, but sharing one task keeps startup and shutdown from
        # racing separate waiters (§5).
        if self._wait_task is not None:
            return await self._wait_task
        return await self._proc.wait()

    async def cancel(self, grace: float = 3.0) -> TerminationResult:
        """Terminate the process and every descendant, returning verified evidence."""

        # Startup identity capture yields the loop, so a cancel arriving mid-startup could otherwise
        # see ``identity`` still ``None`` and refuse to kill a process it actually owns. Wait for the
        # capture to settle first (bounded by the startup timeout), then decide (§5).
        if self._startup_task is not None and not self._startup_task.done():
            with contextlib.suppress(Exception):
                await asyncio.shield(self._startup_task)
        if self._proc is None or self._proc.returncode is not None:
            return TerminationResult(TerminationOutcome.ALREADY_GONE)
        if self._identity is None:
            return TerminationResult(TerminationOutcome.IDENTITY_UNKNOWN)
        result = terminate_process_tree(self._identity, grace=grace)
        # This flag drives terminal reconciliation. Setting it before termination was proven would
        # turn access-denied, identity-mismatch, or survivor outcomes into a false run.cancelled.
        if result.verified_terminated:
            self._cancelled = True
        return result


class TerminationOutcome(str, Enum):
    TERMINATED = "terminated"
    ALREADY_GONE = "already_gone"
    IDENTITY_UNKNOWN = "identity_unknown"
    IDENTITY_MISMATCH = "identity_mismatch"
    ACCESS_DENIED = "access_denied"
    TERMINATION_FAILED = "termination_failed"
    SURVIVORS_REMAINING = "survivors_remaining"


@dataclass(frozen=True)
class TerminationResult:
    """Auditable outcome of a process-tree termination attempt."""

    outcome: TerminationOutcome
    terminated_pids: tuple[int, ...] = ()
    survivors: tuple[int, ...] = ()
    access_denied: tuple[int, ...] = ()
    detail: str = ""

    @property
    def verified_terminated(self) -> bool:
        return self.outcome is TerminationOutcome.TERMINATED and not self.survivors


def _command_identity(argv: Sequence[str]) -> str:
    return hashlib.sha256("\0".join(argv).encode("utf-8", errors="surrogateescape")).hexdigest()


def _capture_process_identity_once(pid: int) -> ProcessIdentity | None:
    if not pid:
        return None
    try:
        process = psutil.Process(pid)
        with process.oneshot():
            create_time = process.create_time()
            executable = process.exe()
            command = process.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return None
    if not executable or not command:
        return None
    return ProcessIdentity(
        pid=pid,
        create_time=create_time,
        executable=str(Path(executable).resolve()),
        command_identity=_command_identity(command),
    )


def capture_process_identity(pid: int | None) -> ProcessIdentity | None:
    """Capture a stable PID/create-time/executable/command identity.

    macOS framework Python and some launcher CLIs re-exec immediately after ``Popen`` returns. A
    single eager sample records the launcher and then rejects the same PID milliseconds later as an
    identity mismatch. Sample through that startup window and return only a stable post-exec value.
    If a very short-lived command exits during the window, its last sample is still useful evidence:
    future verification can only classify it as already gone, never signal a replacement PID.
    """

    if not pid:
        return None
    deadline = time.monotonic() + 0.25
    stable_since = time.monotonic()
    previous = _capture_process_identity_once(pid)
    if previous is None:
        return None
    while time.monotonic() < deadline:
        time.sleep(0.025)
        current = _capture_process_identity_once(pid)
        if current is None:
            return previous
        if current != previous:
            previous = current
            stable_since = time.monotonic()
            continue
        if time.monotonic() - stable_since >= 0.075:
            return current
    return previous


def _capture_owned_process_identity(
    popen: subprocess.Popen[bytes], *, startup_timeout: float = 0.25
) -> ProcessIdentity | None:
    """Capture identity through transient ``psutil`` startup gaps for our exact child handle.

    Linux can briefly expose a live PID before ``exe``/``cmdline`` are readable. Retrying the PID
    alone would risk accepting a replacement if a tiny child exited and its PID were reused. The
    Popen handle closes that race: an exit observed before or after every sample invalidates it.
    """

    deadline = time.monotonic() + startup_timeout
    while True:
        if popen.poll() is not None:
            return None
        identity = capture_process_identity(popen.pid)
        if popen.poll() is not None:
            return None
        if identity is not None:
            return identity
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.01)


def _verify_process(
    identity: ProcessIdentity,
) -> tuple[psutil.Process | None, TerminationResult | None]:
    """Return the matching process or a fail-closed result."""

    try:
        process = psutil.Process(identity.pid)
        with process.oneshot():
            create_time = process.create_time()
            executable = process.exe()
            command = process.cmdline()
    except psutil.NoSuchProcess:
        return None, TerminationResult(TerminationOutcome.ALREADY_GONE)
    except (psutil.AccessDenied, PermissionError):
        return None, TerminationResult(
            TerminationOutcome.ACCESS_DENIED, access_denied=(identity.pid,)
        )
    except (psutil.ZombieProcess, OSError) as exc:
        return None, TerminationResult(TerminationOutcome.IDENTITY_UNKNOWN, detail=str(exc))

    if abs(create_time - identity.create_time) > _IDENTITY_TOLERANCE:
        return None, TerminationResult(TerminationOutcome.IDENTITY_MISMATCH)
    try:
        actual_executable = str(Path(executable).resolve())
    except OSError:
        return None, TerminationResult(TerminationOutcome.IDENTITY_UNKNOWN)
    if (
        actual_executable != identity.executable
        or _command_identity(command) != identity.command_identity
    ):
        return None, TerminationResult(TerminationOutcome.IDENTITY_MISMATCH)
    return process, None


def _signal(proc: psutil.Process, *, terminate: bool) -> TerminationOutcome | None:
    try:
        proc.terminate() if terminate else proc.kill()
        return None
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied:
        return TerminationOutcome.ACCESS_DENIED
    except OSError:
        return TerminationOutcome.TERMINATION_FAILED


def terminate_process_tree(
    identity: ProcessIdentity, *, grace: float = 3.0, kill_grace: float = 3.0
) -> TerminationResult:
    """Terminate an identity-verified process tree and prove every victim is gone."""

    parent, failure = _verify_process(identity)
    if failure is not None:
        return failure
    assert parent is not None
    try:
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        return TerminationResult(TerminationOutcome.ALREADY_GONE)
    except psutil.AccessDenied:
        return TerminationResult(TerminationOutcome.ACCESS_DENIED, access_denied=(identity.pid,))

    victims = [*children, parent]
    denied: list[int] = []
    failures: list[int] = []
    for proc in victims:
        problem = _signal(proc, terminate=True)
        if problem is TerminationOutcome.ACCESS_DENIED:
            denied.append(proc.pid)
        elif problem is TerminationOutcome.TERMINATION_FAILED:
            failures.append(proc.pid)
    gone, alive = psutil.wait_procs(victims, timeout=grace)
    for proc in alive:
        problem = _signal(proc, terminate=False)
        if problem is TerminationOutcome.ACCESS_DENIED:
            denied.append(proc.pid)
        elif problem is TerminationOutcome.TERMINATION_FAILED:
            failures.append(proc.pid)
    killed, survivors = psutil.wait_procs(alive, timeout=kill_grace)
    gone.extend(killed)

    # Re-check rather than trusting wait_procs alone. Process objects protect signal methods against
    # PID reuse, and a fresh is_running check proves no original victim survived the force-kill.
    still_alive: list[int] = []
    for proc in survivors:
        try:
            if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                still_alive.append(proc.pid)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            denied.append(proc.pid)
            still_alive.append(proc.pid)
    still_alive = sorted(set(still_alive))
    if still_alive:
        return TerminationResult(
            TerminationOutcome.SURVIVORS_REMAINING,
            terminated_pids=tuple(sorted({p.pid for p in gone})),
            survivors=tuple(still_alive),
            access_denied=tuple(sorted(set(denied))),
        )
    if denied:
        return TerminationResult(
            TerminationOutcome.ACCESS_DENIED,
            terminated_pids=tuple(sorted({p.pid for p in gone})),
            access_denied=tuple(sorted(set(denied))),
        )
    if failures:
        return TerminationResult(
            TerminationOutcome.TERMINATION_FAILED,
            terminated_pids=tuple(sorted({p.pid for p in gone})),
            detail=f"signals failed for pids {sorted(set(failures))}",
        )
    return TerminationResult(
        TerminationOutcome.TERMINATED,
        terminated_pids=tuple(sorted({p.pid for p in gone})),
    )


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
PID_ALIVE = "alive"  # the same process is still running (PID + create-time match)
PID_GONE = "gone"  # no such PID (or no PID recorded)
PID_REUSED = "reused"  # PID is live but belongs to a *different* process (create-time differs)
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


def process_identity_status(identity: ProcessIdentity | None) -> str:
    """Classify a complete persisted identity without signalling the process."""

    if identity is None:
        return PID_UNKNOWN
    process, failure = _verify_process(identity)
    if process is not None:
        return PID_ALIVE
    assert failure is not None
    if failure.outcome is TerminationOutcome.ALREADY_GONE:
        return PID_GONE
    if failure.outcome is TerminationOutcome.IDENTITY_MISMATCH:
        return PID_REUSED
    return PID_UNKNOWN


def terminate_pid_tree(
    identity: ProcessIdentity | None, *, grace: float = 3.0, kill_grace: float = 3.0
) -> TerminationResult:
    """Strict cross-process entry point: no complete identity means no signal."""

    if identity is None:
        return TerminationResult(TerminationOutcome.IDENTITY_UNKNOWN)
    return terminate_process_tree(identity, grace=grace, kill_grace=kill_grace)


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
    identity = _capture_owned_process_identity(popen)
    if identity is None and popen.poll() is None:
        # Fail closed only while the unidentified process is still alive. Very short commands can
        # exit before psutil's first sample; once ``poll`` has reaped that exact child there is
        # nothing left to signal, and its bounded pipes remain safe to collect through ``popen``.
        popen.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            popen.wait(timeout=5.0)
        raise RuntimeError("could not capture command process identity")
    cmd = argv if shell else list(argv)
    if max_output_bytes is None and cancellation is None:
        try:
            raw_out, raw_err = popen.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if identity is not None:
                terminate_process_tree(identity)
            else:  # defensive: identity-less implies the child was already reaped above
                with contextlib.suppress(ProcessLookupError):
                    popen.kill()
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
        popen, identity=identity, timeout=timeout, limit=limit, cancellation=cancellation
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
    popen: subprocess.Popen,
    *,
    identity: ProcessIdentity | None,
    timeout: float,
    limit: int,
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
        if identity is not None:
            terminate_process_tree(identity)
        elif popen.poll() is None:  # defensive; the caller rejects this state before reading
            with contextlib.suppress(ProcessLookupError):
                popen.kill()
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
