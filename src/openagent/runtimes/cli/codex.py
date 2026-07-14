"""Codex CLI adapter (spec §7).

Runs ``codex exec --json`` and maps its native JSONL stream onto :class:`NormalizedEvent`s.

**Verified live against ``codex-cli 0.142.5``** (see ``tests/fixtures/codex_v0142_*.jsonl``, captured
from real runs). The stream is a thread → turn → item hierarchy::

    {"type": "thread.started", "thread_id": "..."}
    {"type": "turn.started"}
    {"type": "item.started"   , "item": {...}}
    {"type": "item.updated"   , "item": {...}}     # same id — a *replacement*, not an append
    {"type": "item.completed" , "item": {...}}
    {"type": "turn.completed", "usage": {"input_tokens", "cached_input_tokens",
                                         "output_tokens", "reasoning_output_tokens"}}
    {"type": "turn.failed", "error": {"message": "..."}}
    {"type": "error", "message": "..."}

Item types confirmed live (the item's discriminator key is ``type``):

===================  ==========================================================================
``agent_message``    ``{id, type, text}`` — Codex emits *several* per turn (progress narration);
                     the last one is the final answer.
``reasoning``        ``{id, type, text}`` — the model's **reasoning summary**. This is the
                     summary Codex itself exposes, *not* raw chain-of-thought, and it only
                     appears when summaries are requested (``model_reasoning_summary``), which
                     this adapter does. Its text is preserved and shown (item 1).
``todo_list``        ``{id, type, items: [{text, completed}]}` — the plan. Re-sent under the same
                     id as it progresses, so it projects onto one checklist (item 3).
``command_execution````{id, type, command, aggregated_output, exit_code, status}`` — status is
                     ``in_progress`` / ``completed`` / ``failed`` / ``declined``.
                     ``aggregated_output`` is the whole buffer each time → a **snapshot**.
``file_change``      ``{id, type, changes: [{path, kind}], status}`` — kind ``add``/``update``/
                     ``delete``. A non-completed status is a **failed patch**, never a success.
``web_search``       ``{id, type, query, action}`` — ``query`` is empty on ``item.started`` and
                     filled on ``item.completed``.
``mcp_tool_call``    ``{id, type, server, tool, status, arguments, result, error}``
``error``            ``{id, type, message}`` — a non-fatal notice (fatal ones also emit
                     ``turn.failed``).
===================  ==========================================================================

Hidden chain-of-thought is never requested, mapped, or persisted; ``reasoning_output_tokens`` is
counted (as :attr:`TokenUsage.reasoning_tokens`) but the tokens themselves are never obtained.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ...core.events import EventType, ItemStatus, NormalizedEvent
from ...core.models import CliInstallation
from ...core.permissions import get_profile
from ...security.process import ManagedProcess, minimal_environment
from .base import (
    AuthStatus,
    CliCapabilities,
    CliRunRequest,
    StreamOutcome,
    detect_version,
    find_executable,
    run_managed_cli,
)

SOURCE = "codex-cli"

#: The Codex version this adapter's event mapping was captured and verified against (item 16).
VALIDATED_VERSION = "codex-cli 0.142.5"

#: Codex only emits ``reasoning`` items when reasoning summaries are requested — without this the
#: user sees no live reasoning at all. Verified: the default config produced zero reasoning items,
#: ``model_reasoning_summary=detailed`` produced them. Unknown ``-c`` keys are ignored by Codex
#: unless ``--strict-config`` is passed, so this stays safe on other versions.
REASONING_SUMMARY_MODE = "detailed"

#: Per-event cap on captured command output (the projection and artifacts bound it again).
MAX_COMMAND_OUTPUT_CHARS = 8_000

#: Native ``status`` values that mean the item did **not** succeed.
_FAILED_STATUSES = {"failed", "declined", "cancelled", "aborted", "error"}

_CHANGE_EVENT = {
    "add": (EventType.FILE_CREATED, "created"),
    "create": (EventType.FILE_CREATED, "created"),
    "delete": (EventType.FILE_DELETED, "deleted"),
    "remove": (EventType.FILE_DELETED, "deleted"),
    "update": (EventType.FILE_MODIFIED, "modified"),
    "modify": (EventType.FILE_MODIFIED, "modified"),
}


class CodexAdapter:
    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or find_executable("codex")
        self._processes: dict[str, ManagedProcess] = {}

    async def detect(self) -> CliInstallation | None:
        if not self.executable:
            return None
        version = detect_version(self.executable)
        return CliInstallation(
            id="cli_codex", type="codex", executable=self.executable,
            version=version, adapter="codex-json", authenticated=None,
            validated_version=VALIDATED_VERSION,
        )

    async def inspect_auth(self) -> AuthStatus:
        # Codex stores auth under ~/.codex; presence of auth.json indicates a login.
        auth_file = Path.home() / ".codex" / "auth.json"
        if auth_file.exists():
            return AuthStatus(authenticated=True, detail="~/.codex/auth.json present")
        return AuthStatus(authenticated=False, detail="run `codex login` (or set CODEX_API_KEY)")

    async def capabilities(self) -> CliCapabilities:
        return CliCapabilities(
            structured_events=True, resumable=True, edits_files=True, runs_commands=True,
        )

    # ------------------------------------------------------------------ running

    def start_run(self, request: CliRunRequest) -> AsyncIterator[NormalizedEvent]:
        return self._drive(request, self._build_args(request))

    def resume_run(
        self, session_id: str, prompt: str, request: CliRunRequest
    ) -> AsyncIterator[NormalizedEvent]:
        args = [
            self.executable or "codex", "exec", "resume", session_id,
            *self._common_args(request), prompt,
        ]
        return self._drive(request, args)

    def _build_args(self, request: CliRunRequest) -> list[str]:
        return [
            self.executable or "codex", "exec",
            *self._common_args(request),
            request.prompt,
        ]

    def _common_args(self, request: CliRunRequest) -> list[str]:
        sandbox = get_profile(request.permission_profile).codex_sandbox
        args = [
            "--json",
            "--sandbox", sandbox,
            "-c", f"model_reasoning_summary={REASONING_SUMMARY_MODE}",
            "-o", str(self._final_message_path(request)),
        ]
        # A 'copy' worktree is a plain directory, not a git repo; Codex refuses to run outside one
        # unless told otherwise. Only relax the check where it genuinely does not apply.
        if not (request.workspace / ".git").exists():
            args.append("--skip-git-repo-check")
        return args

    def _final_message_path(self, request: CliRunRequest) -> Path:
        """Where Codex writes its last message.

        Deliberately **outside the workspace** (item 6): the old adapter pointed ``-o`` at
        ``<workspace>/.codex-final.txt``, which made an OpenAgent implementation detail show up as a
        file the agent "created" in the user's project diff.
        """

        if request.artifacts_dir is not None:
            request.artifacts_dir.mkdir(parents=True, exist_ok=True)
            return request.artifacts_dir / "codex-final.txt"
        handle, path = tempfile.mkstemp(prefix="openagent-codex-final-", suffix=".txt")
        import os

        os.close(handle)
        return Path(path)

    async def _drive(
        self, request: CliRunRequest, args: list[str]
    ) -> AsyncIterator[NormalizedEvent]:
        if not self.executable:
            yield NormalizedEvent(
                run_id=request.run_id, type=EventType.RUN_FAILED, source=SOURCE,
                data={"error_type": "cli_not_found", "message": "codex is not installed"},
            )
            return

        env = minimal_environment(request.credential_env)
        proc = ManagedProcess(args, cwd=request.workspace, env=env)
        self._processes[request.run_id] = proc
        workspace = request.workspace
        final_path = self._final_message_path(request)

        def mapper(obj: dict[str, Any], run_id: str) -> list[NormalizedEvent]:
            return map_codex_event(obj, run_id, workspace=workspace)

        def finalizer(outcome: StreamOutcome) -> list[NormalizedEvent]:
            return final_message_fallback(request.run_id, final_path, outcome)

        try:
            async for event in run_managed_cli(
                proc=proc, run_id=request.run_id, source=SOURCE, mapper=mapper, finalizer=finalizer,
            ):
                yield event
        finally:
            self._processes.pop(request.run_id, None)

    async def cancel(self, run_id: str) -> None:
        proc = self._processes.get(run_id)
        if proc is not None:
            await proc.cancel()


# --------------------------------------------------------------------------- final-message fallback


def final_message_fallback(
    run_id: str, final_path: Path, outcome: StreamOutcome
) -> list[NormalizedEvent]:
    """Emit Codex's ``--output-last-message`` file as ``message.completed`` when the stream had none.

    Codex always writes its final answer to the ``-o`` file. If the JSONL stream carried a usable
    ``agent_message`` we already have it; otherwise the artifact bundle would show an empty summary
    even though Codex *did* answer. Read the file, emit it as the final message, and delete it —
    it is OpenAgent's scratch file, never a project artifact (item 6).
    """

    text = ""
    try:
        if final_path.exists():
            text = final_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        text = ""
    finally:
        try:
            final_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort cleanup
            pass

    if outcome.saw_message or outcome.cancelled or not text:
        return []
    return [NormalizedEvent(
        run_id=run_id, type=EventType.MESSAGE_COMPLETED, source=SOURCE,
        data={"item_id": "final_message", "status": ItemStatus.COMPLETED.value,
              "text": text[:MAX_COMMAND_OUTPUT_CHARS * 4], "fallback": True},
    )]


# --------------------------------------------------------------------------- event mapping


def map_codex_event(
    obj: dict[str, Any], run_id: str, workspace: Path | None = None
) -> list[NormalizedEvent]:
    """Map one Codex JSONL object to zero or more NormalizedEvents (pure, unit-tested)."""

    etype = obj.get("type", "")

    def ev(t: EventType, **data: Any) -> NormalizedEvent:
        return NormalizedEvent(run_id=run_id, type=t, source=SOURCE, data=data)

    if etype == "thread.started":
        return [ev(EventType.SESSION_CREATED, provider_session_id=obj.get("thread_id"))]
    if etype == "turn.started":
        return [ev(EventType.RUN_PHASE, phase="running")]
    if etype == "turn.completed":
        usage = obj.get("usage") or {}
        return [ev(
            EventType.USAGE_UPDATED,
            input_tokens=int(usage.get("input_tokens") or 0),
            cached_input_tokens=int(usage.get("cached_input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            # Codex names it reasoning_output_tokens; OpenAgent normalizes to reasoning_tokens.
            reasoning_tokens=int(usage.get("reasoning_output_tokens") or 0),
        ), ev(EventType.RUN_COMPLETED)]
    if etype == "turn.failed":
        message = _error_message((obj.get("error") or {}).get("message"))
        return [ev(EventType.RUN_FAILED, error_type=classify_error(message), message=message)]
    if etype == "error":
        message = _error_message(obj.get("message"))
        return [ev(EventType.LOG, level="error", message=message)]
    if etype in ("item.started", "item.updated", "item.completed"):
        return _map_item(obj, etype, ev, workspace)
    return [ev(EventType.LOG, raw_type=etype)]


def _map_item(
    obj: dict[str, Any], etype: str, ev: Any, workspace: Path | None
) -> list[NormalizedEvent]:
    item = obj.get("item") or {}
    itype = item.get("type") or item.get("item_type") or ""
    item_id = str(item.get("id") or "")
    completed = etype == "item.completed"

    if itype in ("agent_message", "assistant_message"):
        # Codex emits several agent messages per turn; each is a completed, user-visible message.
        text = str(item.get("text") or "")
        if not completed or not text.strip():
            return []
        return [ev(EventType.MESSAGE_COMPLETED, item_id=item_id,
                   status=ItemStatus.COMPLETED.value, text=text)]

    if itype == "reasoning":
        # The reasoning **summary** Codex itself exposes (never raw chain-of-thought, item 1).
        text = str(item.get("text") or "").strip()
        if not text:
            return []  # never render a blank summary
        status = ItemStatus.COMPLETED.value if completed else ItemStatus.IN_PROGRESS.value
        return [ev(EventType.REASONING_SUMMARY, item_id=item_id, status=status, text=text,
                   title="Reasoning summary")]

    if itype in ("todo_list", "plan"):
        entries = [
            {"text": str(x.get("text") or ""), "completed": bool(x.get("completed"))}
            for x in (item.get("items") or []) if isinstance(x, dict)
        ]
        status = ItemStatus.COMPLETED.value if completed else ItemStatus.IN_PROGRESS.value
        return [ev(EventType.PLAN_UPDATED, item_id=item_id, status=status, items=entries)]

    if itype in ("command_execution", "command"):
        return _map_command(item, item_id, etype, completed, ev)

    if itype in ("file_change", "patch", "file_update"):
        return _map_file_change(item, item_id, completed, ev, workspace)

    if itype == "web_search":
        query = str(item.get("query") or "")
        if completed:
            return [ev(EventType.WEB_SEARCH_COMPLETED, item_id=item_id,
                       status=ItemStatus.COMPLETED.value, query=query)]
        if etype == "item.started":
            # The query is empty on start and arrives on completion — don't show a blank search.
            return [ev(EventType.WEB_SEARCH_STARTED, item_id=item_id,
                       status=ItemStatus.IN_PROGRESS.value, query=query)]
        return []

    if itype in ("mcp_tool_call", "tool_call"):
        return _map_tool_call(item, item_id, etype, completed, ev)

    if itype == "error":
        return [ev(EventType.LOG, level="error", item_id=item_id,
                   message=_error_message(item.get("message")))]

    if completed:
        return [ev(EventType.LOG, item_type=itype, item_id=item_id)]
    return []


def _map_command(
    item: dict[str, Any], item_id: str, etype: str, completed: bool, ev: Any
) -> list[NormalizedEvent]:
    command = str(item.get("command") or "")
    native = str(item.get("status") or "").lower()
    output = str(item.get("aggregated_output") or "")[-MAX_COMMAND_OUTPUT_CHARS:]
    exit_code = item.get("exit_code")

    if etype == "item.started":
        return [ev(EventType.COMMAND_STARTED, item_id=item_id,
                   status=ItemStatus.IN_PROGRESS.value, command=command)]

    if etype == "item.updated":
        # ``aggregated_output`` is the *whole* buffer each time, not a delta: mark it a snapshot so
        # readers replace the visible output rather than appending the full buffer again (item 5).
        if not output:
            return []
        return [ev(EventType.COMMAND_OUTPUT, item_id=item_id,
                   status=ItemStatus.IN_PROGRESS.value, command=command,
                   output=output, snapshot=True)]

    # completed — a failed/declined command must never look successful (item 5).
    status = _command_status(native, exit_code)
    return [ev(EventType.COMMAND_COMPLETED, item_id=item_id, status=status, command=command,
               exit_code=exit_code, output=output, snapshot=True, native_status=native or None)]


def _command_status(native: str, exit_code: Any) -> str:
    if native in _FAILED_STATUSES:
        return ItemStatus.FAILED.value
    if native == "completed":
        # Codex reports 'completed' for a command that ran — success still depends on the exit code.
        return (ItemStatus.COMPLETED.value if exit_code in (0, None)
                else ItemStatus.FAILED.value)
    if exit_code is None:
        return ItemStatus.COMPLETED.value
    return ItemStatus.COMPLETED.value if exit_code == 0 else ItemStatus.FAILED.value


def _map_file_change(
    item: dict[str, Any], item_id: str, completed: bool, ev: Any, workspace: Path | None
) -> list[NormalizedEvent]:
    native = str(item.get("status") or "").lower()
    changes = item.get("changes") or []
    if not isinstance(changes, list):
        return []

    if completed:
        status = (ItemStatus.FAILED.value if native in _FAILED_STATUSES
                  else ItemStatus.COMPLETED.value)
    else:
        status = ItemStatus.IN_PROGRESS.value

    events: list[NormalizedEvent] = []
    for index, change in enumerate(changes):
        if not isinstance(change, dict):
            continue
        kind = str(change.get("kind") or change.get("type") or "update").lower()
        event_type, verb = _CHANGE_EVENT.get(kind, (EventType.FILE_MODIFIED, "modified"))
        path = _relative_path(str(change.get("path") or ""), workspace)
        # One file_change item can touch several paths; give each its own addressable id so
        # started→completed updates the *same* file's card without collapsing the others (item 3).
        events.append(ev(
            event_type, item_id=f"{item_id}#{index}", status=status, path=path, change=verb,
            native_status=native or None,
        ))
    return events


def _map_tool_call(
    item: dict[str, Any], item_id: str, etype: str, completed: bool, ev: Any
) -> list[NormalizedEvent]:
    tool = str(item.get("tool") or item.get("name") or "")
    server = str(item.get("server") or "")
    native = str(item.get("status") or "").lower()
    data: dict[str, Any] = {"item_id": item_id, "tool": tool}
    if server:
        data["server"] = server

    if etype == "item.started":
        return [ev(EventType.TOOL_STARTED, status=ItemStatus.IN_PROGRESS.value,
                   arguments_summary=_summarize(item.get("arguments")), **data)]
    if not completed:
        return []

    failed = native in _FAILED_STATUSES or bool(item.get("error"))
    if failed:
        return [ev(EventType.TOOL_FAILED, status=ItemStatus.FAILED.value,
                   error=_summarize(item.get("error")) or "tool call failed", **data)]
    return [ev(EventType.TOOL_COMPLETED, status=ItemStatus.COMPLETED.value,
               result_summary=_summarize(item.get("result")), **data)]


# --------------------------------------------------------------------------- helpers

#: Bound on any summarized MCP payload — arbitrary tool arguments/results are never persisted whole.
_SUMMARY_LIMIT = 500


def _summarize(value: Any) -> str:
    """A short, bounded, string form of an arbitrary tool payload (never the unbounded original)."""

    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            text = str(value)
    text = " ".join(text.split())
    return text[:_SUMMARY_LIMIT] + ("…" if len(text) > _SUMMARY_LIMIT else "")


def _relative_path(path: str, workspace: Path | None) -> str:
    """Show a workspace-relative path; absolute paths leak the machine's layout into artifacts."""

    if not path or workspace is None:
        return path
    try:
        return str(Path(path).relative_to(workspace))
    except ValueError:
        return path


def _error_message(raw: Any) -> str:
    """Codex nests the provider's JSON error inside a string; surface the human-readable part."""

    if raw is None:
        return ""
    text = str(raw)
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return text
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])
            if payload.get("message"):
                return str(payload["message"])
    return text


def classify_error(message: str) -> str:
    """Map a Codex error message onto OpenAgent's normalized failure types (item 13)."""

    low = (message or "").lower()
    if not low:
        return "unknown"
    if "usage limit" in low or "rate limit" in low or "429" in low or "quota" in low:
        return "provider_rate_limited"
    if "insufficient" in low and ("credit" in low or "balance" in low or "fund" in low):
        return "insufficient_balance"
    if any(x in low for x in ("unauthorized", "authentication", "not logged in", "401",
                              "invalid api key", "please sign in")):
        return "authentication_failed"
    if "context" in low and ("length" in low or "window" in low or "too long" in low):
        return "context_limit"
    if "not supported" in low or "requires a newer version" in low or "not found" in low:
        return "schema_mismatch"
    return "unknown"
