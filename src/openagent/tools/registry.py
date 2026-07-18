"""Tool registry + executor (spec §27).

Defines each built-in tool with a JSON-Schema parameter spec (the shape provider adapters translate
into their native tool-calling format) and exposes:

* :func:`schemas_for_profile` — the tools an API agent may see, filtered by permission profile.
* :class:`ToolExecutor` — runs a tool call, translating exceptions into failed :class:`ToolResult`s.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jsonschema import ValidationError, validate

from ..core.cancellation import RunCancelled
from ..core.events import ToolCall
from ..core.limits import RUNTIME_LIMITS
from ..core.permissions import PermissionProfile
from ..credentials.redaction import redact
from ..security.execution_backend import ExecutionBackendError
from ..security.filesystem import WorkspaceBudgetExceeded
from ..security.process import OutputLimitExceeded
from . import control, fs, git
from . import exec as exec_tools
from .base import ToolContext, ToolError, ToolExecutionInternalError, ToolResult

ToolHandler = Callable[..., ToolResult]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def schema(self) -> dict[str, Any]:
        """Provider-neutral function schema (adapters map this to OpenAI/Anthropic shapes)."""
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


_STR = {"type": "string", "maxLength": 65_536}
_PATH = {"type": "string", "minLength": 1, "maxLength": 4_096}
_CONTENT = {"type": "string", "maxLength": RUNTIME_LIMITS.model_text_bytes}
_INT = {"type": "integer", "minimum": 0, "maximum": 50_000}
_TIMEOUT = {"type": "integer", "minimum": 1, "maximum": 3_600}
_BOOL = {"type": "boolean"}


ALL_TOOLS: dict[str, Tool] = {
    "list_files": Tool(
        "list_files",
        "List files under a directory (workspace-relative).",
        _obj({"path": _PATH, "depth": {"type": "integer", "minimum": 0, "maximum": 64}}, []),
        fs.list_files,
    ),
    "read_file": Tool(
        "read_file",
        "Read a UTF-8 text file (workspace-relative path).",
        _obj({"path": _PATH}, ["path"]),
        fs.read_file,
    ),
    "search_files": Tool(
        "search_files",
        "Find files by glob pattern (e.g. '*.py').",
        _obj({"pattern": _STR}, ["pattern"]),
        fs.search_files,
    ),
    "search_text": Tool(
        "search_text",
        "Search file contents for a substring; optional glob filter.",
        _obj({"query": _STR, "glob": _STR}, ["query"]),
        fs.search_text,
    ),
    "write_file": Tool(
        "write_file",
        "Write/overwrite a file with full content. Prefer apply_patch for edits.",
        _obj({"path": _PATH, "content": _CONTENT}, ["path", "content"]),
        fs.write_file,
    ),
    "apply_patch": Tool(
        "apply_patch",
        "Replace a unique old_string with new_string in a file (targeted, reviewable edit).",
        _obj(
            {
                "path": _PATH,
                "old_string": _CONTENT,
                "new_string": _CONTENT,
                "replace_all": _BOOL,
            },
            ["path", "old_string", "new_string"],
        ),
        fs.apply_patch,
    ),
    "run_command": Tool(
        "run_command",
        "Run a shell command in the workspace (screened by the command policy).",
        _obj({"command": _STR, "timeout": _TIMEOUT}, ["command"]),
        exec_tools.run_command,
    ),
    "run_tests": Tool(
        "run_tests",
        "Run tests from structured argv (default: ['pytest', '-q']).",
        _obj(
            {
                "argv": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 4_096},
                    "maxItems": 256,
                },
                "timeout": _TIMEOUT,
            },
            [],
        ),
        exec_tools.run_tests,
    ),
    "git_status": Tool("git_status", "Show git status (short).", _obj({}, []), git.git_status),
    "git_diff": Tool(
        "git_diff",
        "Show the git diff, optionally for one path.",
        _obj({"path": _PATH}, []),
        git.git_diff,
    ),
    "ask_user": Tool(
        "ask_user",
        "Ask the user a clarifying question.",
        _obj({"question": _STR}, ["question"]),
        control.ask_user,
    ),
    "update_plan": Tool(
        "update_plan",
        "Publish your current plan as a checklist the user can watch. Call it once you have a plan, "
        "and again as you complete steps. Send the whole checklist each time.",
        _obj(
            {
                "items": {
                    "type": "array",
                    "items": _obj({"text": _STR, "completed": _BOOL}, ["text"]),
                    "maxItems": 100,
                }
            },
            ["items"],
        ),
        control.update_plan,
    ),
    "report_progress": Tool(
        "report_progress",
        "Tell the user, in one or two plain sentences, what you found and what you are doing next. "
        "Use it before major phases, not for every small step. Never reveal private reasoning.",
        _obj({"summary": _STR, "next_step": _STR}, ["summary"]),
        control.report_progress,
    ),
    "finish_task": Tool(
        "finish_task",
        "Finish the task with a short summary of what was done.",
        _obj({"summary": _STR}, ["summary"]),
        control.finish_task,
    ),
}


def tools_for_profile(profile: PermissionProfile) -> list[Tool]:
    return [ALL_TOOLS[name] for name in ALL_TOOLS if name in profile.allowed_tools]


def schemas_for_profile(profile: PermissionProfile) -> list[dict[str, Any]]:
    return [tool.schema() for tool in tools_for_profile(profile)]


class ToolExecutor:
    """Executes tool calls against a :class:`ToolContext`."""

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    def execute(self, call: ToolCall) -> ToolResult:
        tool = ALL_TOOLS.get(call.name)
        if tool is None:
            return ToolResult.failure(
                f"unknown tool: {redact(call.name)}", error_type="unknown_tool"
            )
        if tool.name not in self.ctx.profile.allowed_tools:
            return ToolResult.failure(
                f"tool {redact(call.name)!r} is not permitted by this profile",
                error_type="permission_denied",
            )
        try:
            encoded = json.dumps(call.arguments, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            return ToolResult.failure(
                f"invalid arguments for {redact(call.name)}: {redact(str(exc))}",
                error_type="invalid_arguments",
            )
        if len(encoded) > RUNTIME_LIMITS.tool_arguments_bytes:
            return ToolResult.failure(
                f"invalid arguments for {call.name}: exceeds "
                f"{RUNTIME_LIMITS.tool_arguments_bytes} bytes",
                error_type="invalid_arguments",
            )
        try:
            validate(instance=call.arguments, schema=tool.parameters)
        except ValidationError as exc:
            return ToolResult.failure(
                f"invalid arguments for {redact(call.name)}: {redact(exc.message)}",
                error_type="invalid_arguments",
            )
        try:
            return tool.handler(self.ctx, **call.arguments)
        except (control.TaskFinished, RunCancelled):
            raise
        except ToolError as exc:
            return _operational_failure("tool_error", exc)
        except PermissionError as exc:
            return _operational_failure("permission_denied", exc)
        except WorkspaceBudgetExceeded as exc:
            return _operational_failure("workspace_budget_exceeded", exc)
        except ExecutionBackendError as exc:
            return _operational_failure("execution_backend_error", exc)
        except OutputLimitExceeded as exc:
            return _operational_failure("output_limit_exceeded", exc)
        except UnicodeError as exc:
            return _operational_failure("encoding_error", exc)
        except OSError as exc:
            return _operational_failure("os_error", exc)
        except Exception as exc:
            # Schema validation already established the call shape. A TypeError/AssertionError/etc.
            # raised *inside* the handler is therefore a programmer/invariant bug, not bad model
            # input. Chain it for local diagnostics but expose only the normalized safe exception.
            raise ToolExecutionInternalError(tool.name) from exc


def _operational_failure(error_type: str, exc: BaseException) -> ToolResult:
    """Turn an expected host/backend failure into bounded, redacted model-visible output."""

    detail = redact(str(exc)).strip()[:1_000] or error_type.replace("_", " ")
    return ToolResult.failure(detail, error_type=error_type)
