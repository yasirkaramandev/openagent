"""Antigravity CLI adapter (Google Antigravity ``agy``).

Verified live against **agy v1.1.0** on 2026-07-13 (see ``tests/fixtures/antigravity_print.jsonl``):

    agy --print "<prompt>" --output-format json [--conversation <id>]

emits a **single** JSON result object (not a streaming feed)::

    {"conversation_id": "...", "status": "SUCCESS", "response": "...",
     "duration_seconds": 3.4, "num_turns": 1,
     "usage": {"input_tokens": ..., "output_tokens": ..., "thinking_tokens": ..., "total_tokens": ...}}

Confirmed live: ``--print`` non-interactive execution (exit 0 on SUCCESS), ``--conversation <id>``
resume (preserves ``conversation_id``, increments ``num_turns``, retains memory), and ``agy models``
auth. The ``status`` enum seen in the binary is ``SUCCESS`` / ``ABORTED`` / ``CANCELLED`` / ``UNKNOWN``
with ``error`` / ``message`` fields on failure. Antigravity reports **no monetary cost** (subscription),
so ``provider_cost`` stays ``None``, and — because the output is a single final object — only coarse
events are available (final text + usage + terminal status), never per-file/per-command events.

Because the output is one object, the mapper is fail-closed: only an explicit ``SUCCESS`` completes;
``CANCELLED`` maps to cancelled; anything else (``ABORTED`` / ``UNKNOWN`` / missing / error) fails. The
shared ``run_managed_cli`` finalizer then reconciles that against the process exit code (spec §6.2).

**Permissions (item 15).** ``--print`` is non-interactive, so it cannot answer Antigravity's own
tool-permission prompt; the only way to let it edit is ``--dangerously-skip-permissions``, which
disables *Antigravity's* checks — and OpenAgent cannot observe Antigravity's internal tool calls to
compensate. v0.1 therefore refuses to infer that from a ``safe-edit`` profile:

* ``read-only`` → ``--mode plan`` (supported, the default);
* ``safe-edit`` → editing is **experimental and off** unless ``OPENAGENT_ANTIGRAVITY_EXPERIMENTAL_EDIT``
  is set;
* ``development`` / ``full-access`` → the bypass is used only when ``OPENAGENT_ANTIGRAVITY_DANGEROUS_BYPASS``
  is set, and the run emits a loud warning.

A blocked combination fails at preflight with an actionable reason rather than starting a run that
silently cannot do what was asked.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ...core.events import EventType, NormalizedEvent
from ...core.models import CliInstallation
from ...core.permissions import get_profile
from ...security.process import ManagedProcess, minimal_environment
from .base import (
    AuthStatus,
    CliCapabilities,
    CliRunRequest,
    detect_version,
    find_executable,
    run_managed_cli,
)

SOURCE = "antigravity-cli"

#: The Antigravity version this adapter's output mapping was captured against (item 16).
VALIDATED_VERSION = "1.1.0"

#: Opt-in to let Antigravity **edit files** at all. Editing requires
#: ``--dangerously-skip-permissions`` because ``--print`` is non-interactive and cannot answer
#: Antigravity's own tool prompt — and that flag turns *Antigravity's* permission checks off while
#: OpenAgent cannot observe its internal tool calls. So OpenAgent v0.1 does **not** infer it from a
#: ``safe-edit`` profile (item 15): editing is experimental and must be enabled deliberately.
EXPERIMENTAL_EDIT_ENV = "OPENAGENT_ANTIGRAVITY_EXPERIMENTAL_EDIT"

#: A second, separate opt-in for the high-risk profiles (development / full-access).
DANGEROUS_BYPASS_ENV = "OPENAGENT_ANTIGRAVITY_DANGEROUS_BYPASS"

#: Profiles whose semantics need Antigravity's native permission bypass to be honored at all.
_HIGH_RISK_PROFILES = {"development", "full-access"}


class AntigravityPermissionError(RuntimeError):
    """The requested permission profile needs an Antigravity capability the user has not enabled."""


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _run_agy_models(executable: str) -> list[str]:
    """Run ``agy models`` and return the model labels it prints (one per line). Raises on failure."""

    try:
        result = subprocess.run(
            [executable, "models"], capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"`agy models` did not run: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or f"exit code {result.returncode}"
        raise RuntimeError(f"`agy models` failed: {detail[:200]}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


class AntigravityAdapter:
    def __init__(
        self,
        executable: str | None = None,
        *,
        allow_experimental_edit: bool | None = None,
        allow_dangerous_bypass: bool | None = None,
    ) -> None:
        # The official executable is ``agy``; ``antigravity`` is accepted as an alias if present.
        self.executable = executable or find_executable("agy", "antigravity")
        self._processes: dict[str, ManagedProcess] = {}
        self.allow_experimental_edit = (
            _env_flag(EXPERIMENTAL_EDIT_ENV) if allow_experimental_edit is None
            else allow_experimental_edit
        )
        self.allow_dangerous_bypass = (
            _env_flag(DANGEROUS_BYPASS_ENV) if allow_dangerous_bypass is None
            else allow_dangerous_bypass
        )

    async def detect(self) -> CliInstallation | None:
        if not self.executable:
            return None
        return CliInstallation(
            id="cli_antigravity", type="antigravity", executable=self.executable,
            version=detect_version(self.executable), adapter="antigravity-json",
            authenticated=None, experimental=False, validated_version=VALIDATED_VERSION,
        )

    async def inspect_auth(self) -> AuthStatus:
        # Offline best-effort: the antigravity-cli state file indicates a configured/signed-in CLI.
        state = Path.home() / ".gemini" / "antigravity-cli" / "jetski_state.pbtxt"
        if state.exists():
            return AuthStatus(authenticated=True, detail="~/.gemini/antigravity-cli state present")
        return AuthStatus(authenticated=False, detail="run `agy` to sign in")

    async def capabilities(self) -> CliCapabilities:
        # Structured JSON result + resume are verified live. But editing is NOT a normal, safe,
        # always-on capability here (item 9): file edits / command execution only happen through
        # Antigravity's own tools, reachable non-interactively **only** via
        # ``--dangerously-skip-permissions`` — an experimental, opt-in path (see module docstring).
        # So ``edits_files``/``runs_commands`` reflect whether that opt-in is actually enabled, and
        # the adapter is flagged ``experimental`` rather than pretending editing is verified & safe.
        editing = self.allow_experimental_edit or self.allow_dangerous_bypass
        return CliCapabilities(
            structured_events=True, resumable=True, edits_files=editing, runs_commands=editing,
            experimental=True,
        )

    #: How selectable models are discovered for this CLI, shown in the wizard (Phase 4). Antigravity
    #: is the one first-class CLI that can enumerate models offline.
    model_discovery_method = "agy models"

    async def list_models(self) -> list[str]:
        """Discover selectable models by parsing ``agy models`` (verified live against agy v1.1.1).

        Real command, not a hard-coded list: ``agy models`` prints one model label per line
        (e.g. ``Gemini 3.5 Flash (Medium)``). A non-zero exit or missing executable raises, so the
        wizard can fall back to a manual id / the CLI's own default instead of showing a fake list.
        """

        if not self.executable:
            raise RuntimeError("antigravity (agy) is not installed")
        return await asyncio.to_thread(_run_agy_models, self.executable)

    # ------------------------------------------------------------------ running

    def start_run(self, request: CliRunRequest) -> AsyncIterator[NormalizedEvent]:
        return self._drive(request, prompt=request.prompt)

    def resume_run(
        self, session_id: str, prompt: str, request: CliRunRequest
    ) -> AsyncIterator[NormalizedEvent]:
        return self._drive(request, prompt=prompt, conversation=session_id)

    def _build_args(
        self, request: CliRunRequest, prompt: str, *, conversation: str | None = None
    ) -> list[str]:
        args = [self.executable or "agy", "--print", prompt, "--output-format", "json"]
        if request.model:
            # Pin the selected model (verified: `--model "<label>"` in agy --help, and the labels
            # from `agy models` are accepted verbatim). Without this a model discovered/pinned in the
            # wizard would be stored on the agent yet silently ignored — agy would use its default.
            args += ["--model", request.model]
        if conversation:
            args += ["--conversation", conversation]
        args += self._permission_args(request.permission_profile)
        return args

    def permission_status(self, profile_name: str) -> tuple[bool, str]:
        """Whether ``profile_name`` can run on Antigravity right now, and why not (item 15).

        Used by preflight so a blocked run is refused *before* it starts, with an actionable reason.
        """

        profile = get_profile(profile_name)
        if not profile.can_edit_files:
            return True, "read-only → Antigravity plan mode"
        if profile.name in _HIGH_RISK_PROFILES:
            if self.allow_dangerous_bypass:
                return True, (
                    f"{profile.name} → native permission bypass ENABLED via "
                    f"{DANGEROUS_BYPASS_ENV} (high risk)"
                )
            return False, (
                f"profile {profile.name!r} needs Antigravity's native permission bypass "
                f"(--dangerously-skip-permissions), which disables Antigravity's own tool checks "
                f"while OpenAgent cannot observe its internal tool calls. Set "
                f"{DANGEROUS_BYPASS_ENV}=1 to accept that risk, or choose the read-only profile."
            )
        if self.allow_experimental_edit:
            return True, (
                f"{profile.name} → experimental editing ENABLED via {EXPERIMENTAL_EDIT_ENV}"
            )
        return False, (
            f"Antigravity editing is EXPERIMENTAL in v0.1 and is not enabled. A non-interactive "
            f"--print run can only edit with --dangerously-skip-permissions, which turns off "
            f"Antigravity's own permission checks; OpenAgent will not do that just because the "
            f"profile is {profile.name!r}. Set {EXPERIMENTAL_EDIT_ENV}=1 to opt in, or choose the "
            f"read-only profile."
        )

    def _permission_args(self, profile_name: str) -> list[str]:
        """Map the permission profile onto Antigravity's flags — conservatively (item 15).

        ``read-only`` runs in ``--mode plan``. An editing profile is **refused** unless the user has
        explicitly opted in, because the only way to edit non-interactively is
        ``--dangerously-skip-permissions``: that disables Antigravity's *own* permission checks, and
        OpenAgent cannot see Antigravity's internal tool calls to compensate. A ``safe-edit`` profile
        alone can never imply that bypass — "safe" would be a lie.
        """

        profile = get_profile(profile_name)
        if not profile.can_edit_files:
            return ["--mode", "plan"]
        allowed, reason = self.permission_status(profile_name)
        if not allowed:
            raise AntigravityPermissionError(reason)
        return ["--dangerously-skip-permissions"]

    async def _drive(
        self, request: CliRunRequest, *, prompt: str, conversation: str | None = None
    ) -> AsyncIterator[NormalizedEvent]:
        if not self.executable:
            yield NormalizedEvent(
                run_id=request.run_id, type=EventType.RUN_FAILED, source=SOURCE,
                data={"error_type": "cli_not_found", "message": "antigravity (agy) is not installed"},
            )
            return
        try:
            args = self._build_args(request, prompt, conversation=conversation)
        except AntigravityPermissionError as exc:
            yield NormalizedEvent(
                run_id=request.run_id, type=EventType.RUN_FAILED, source=SOURCE,
                data={"error_type": "permission_mode_unsupported", "message": str(exc)},
            )
            return
        if self.allow_experimental_edit or self.allow_dangerous_bypass:
            _, reason = self.permission_status(request.permission_profile)
            if get_profile(request.permission_profile).can_edit_files:
                yield NormalizedEvent(
                    run_id=request.run_id, type=EventType.LOG, source=SOURCE,
                    data={"level": "warning", "message":
                          "Antigravity is running with its native permission checks DISABLED "
                          f"({reason}). OpenAgent cannot observe its internal tool calls."},
                )
        env = minimal_environment(request.credential_env)
        proc = ManagedProcess(args, cwd=request.workspace, env=env)
        self._processes[request.run_id] = proc
        try:
            async for event in run_managed_cli(
                proc=proc, run_id=request.run_id, source=SOURCE, mapper=map_antigravity_event
            ):
                yield event
        finally:
            self._processes.pop(request.run_id, None)

    async def cancel(self, run_id: str) -> None:
        proc = self._processes.get(run_id)
        if proc is not None:
            await proc.cancel()


def map_antigravity_event(obj: dict[str, Any], run_id: str) -> list[NormalizedEvent]:
    """Map Antigravity's single ``--output-format json`` result object to NormalizedEvents (pure).

    Fail-closed on ``status``: only ``SUCCESS`` completes; ``CANCELLED`` cancels; everything else
    (``ABORTED`` / ``UNKNOWN`` / missing / an ``error``) fails. The exit-code reconciliation in
    ``run_managed_cli`` is the second safety net.
    """

    def ev(t: EventType, **data: Any) -> NormalizedEvent:
        return NormalizedEvent(run_id=run_id, type=t, source=SOURCE, data=data)

    events: list[NormalizedEvent] = []
    conversation = obj.get("conversation_id")
    if conversation:
        events.append(ev(EventType.SESSION_CREATED, provider_session_id=conversation))

    response = obj.get("response")
    if isinstance(response, str) and response.strip():
        events.append(ev(EventType.MESSAGE_COMPLETED, text=response))

    usage = obj.get("usage")
    if isinstance(usage, dict):
        events.append(ev(
            EventType.USAGE_UPDATED,
            input_tokens=int(usage.get("input_tokens") or 0),
            cached_input_tokens=0,  # Antigravity reports no cached tokens
            output_tokens=int(usage.get("output_tokens") or 0),
            # Antigravity names reasoning tokens "thinking_tokens"; OpenAgent normalizes to
            # reasoning_tokens so the usage schema matches every other backend (codex/api) — item 9.
            reasoning_tokens=int(usage.get("thinking_tokens") or 0),
            provider_cost=None,  # subscription product — no per-run monetary cost is reported
        ))

    status = str(obj.get("status") or "").upper()
    if status == "SUCCESS":
        events.append(ev(EventType.RUN_COMPLETED, result=response if isinstance(response, str) else ""))
    elif status == "CANCELLED":
        events.append(ev(EventType.RUN_CANCELLED,
                         reason=str(obj.get("message") or "antigravity reported cancelled")))
    else:
        message = obj.get("error") or obj.get("message") or f"antigravity reported {status or 'no status'}"
        events.append(ev(EventType.RUN_FAILED, error_type="antigravity_error", message=str(message)))
    return events
