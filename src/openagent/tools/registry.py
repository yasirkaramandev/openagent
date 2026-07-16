"""Tool registry + executor (spec §27).

Defines each built-in tool with a JSON-Schema parameter spec (the shape provider adapters translate
into their native tool-calling format) and exposes:

* :func:`schemas_for_profile` — the tools an API agent may see, filtered by permission profile.
* :class:`ToolExecutor` — runs a tool call, translating exceptions into failed :class:`ToolResult`s.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..core.events import ToolCall
from ..core.permissions import PermissionProfile
from . import control, fs, git
from . import exec as exec_tools
from .base import ToolContext, ToolError, ToolResult

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
    return {"type": "object", "properties": props, "required": required}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}


ALL_TOOLS: dict[str, Tool] = {
    "list_files": Tool(
        "list_files",
        "List files under a directory (workspace-relative).",
        _obj({"path": _STR, "depth": _INT}, []),
        fs.list_files,
    ),
    "read_file": Tool(
        "read_file",
        "Read a UTF-8 text file (workspace-relative path).",
        _obj({"path": _STR}, ["path"]),
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
        _obj({"path": _STR, "content": _STR}, ["path", "content"]),
        fs.write_file,
    ),
    "apply_patch": Tool(
        "apply_patch",
        "Replace a unique old_string with new_string in a file (targeted, reviewable edit).",
        _obj(
            {"path": _STR, "old_string": _STR, "new_string": _STR, "replace_all": _BOOL},
            ["path", "old_string", "new_string"],
        ),
        fs.apply_patch,
    ),
    "run_command": Tool(
        "run_command",
        "Run a shell command in the workspace (screened by the command policy).",
        _obj({"command": _STR, "timeout": _INT}, ["command"]),
        exec_tools.run_command,
    ),
    "run_tests": Tool(
        "run_tests",
        "Run the test suite (default: 'pytest -q').",
        _obj({"command": _STR, "timeout": _INT}, []),
        exec_tools.run_tests,
    ),
    "git_status": Tool("git_status", "Show git status (short).", _obj({}, []), git.git_status),
    "git_diff": Tool(
        "git_diff",
        "Show the git diff, optionally for one path.",
        _obj({"path": _STR}, []),
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
            return ToolResult.failure(f"unknown tool: {call.name}")
        if tool.name not in self.ctx.profile.allowed_tools:
            return ToolResult.failure(f"tool {call.name!r} is not permitted by this profile")
        try:
            return tool.handler(self.ctx, **call.arguments)
        except ToolError as exc:
            return ToolResult.failure(str(exc))
        except TypeError as exc:  # bad/missing arguments from the model
            return ToolResult.failure(f"invalid arguments for {call.name}: {exc}")
