import asyncio
from pathlib import Path

import pytest

from openagent.core.cancellation import RunCancelled
from openagent.core.events import ToolCall
from openagent.core.permissions import DEVELOPMENT, READ_ONLY, SAFE_EDIT, get_profile
from openagent.credentials.redaction import secret_scope
from openagent.security.approvals import ApprovalGate
from openagent.security.execution_backend import ExecutionBackendError
from openagent.security.filesystem import WorkspaceBudgetExceeded
from openagent.security.process import OutputLimitExceeded
from openagent.tools.base import ToolContext, ToolError, ToolExecutionInternalError
from openagent.tools.control import TaskFinished
from openagent.tools.registry import ALL_TOOLS, Tool, ToolExecutor, schemas_for_profile


def make_ctx(root: Path, profile_name: str = SAFE_EDIT) -> ToolContext:
    return ToolContext(
        workspace_root=root,
        profile=get_profile(profile_name),
        approval_gate=ApprovalGate(auto_approve=False),
        run_id="run_test",
    )


def test_path_traversal_rejected(tmp_path: Path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(ToolError):
        ctx.resolve_path("../../etc/passwd")


def test_absolute_escape_rejected(tmp_path: Path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(ToolError):
        ctx.resolve_path("/etc/passwd")


def test_read_and_write_roundtrip(tmp_path: Path):
    (tmp_path / "main.py").write_text("print('hi')\n")
    ctx = make_ctx(tmp_path)
    execu = ToolExecutor(ctx)
    read = execu.execute(ToolCall(id="1", name="read_file", arguments={"path": "main.py"}))
    assert "print('hi')" in read.content
    patched = execu.execute(
        ToolCall(
            id="2",
            name="apply_patch",
            arguments={"path": "main.py", "old_string": "hi", "new_string": "hello"},
        )
    )
    assert patched.ok
    assert "hello" in (tmp_path / "main.py").read_text()


def test_apply_patch_requires_unique(tmp_path: Path):
    (tmp_path / "f.txt").write_text("a\na\n")
    execu = ToolExecutor(make_ctx(tmp_path))
    result = execu.execute(
        ToolCall(
            id="1",
            name="apply_patch",
            arguments={"path": "f.txt", "old_string": "a", "new_string": "b"},
        )
    )
    assert not result.ok
    assert "unique" in result.content


def test_read_only_profile_blocks_writes(tmp_path: Path):
    (tmp_path / "f.txt").write_text("x")
    execu = ToolExecutor(make_ctx(tmp_path, READ_ONLY))
    result = execu.execute(
        ToolCall(
            id="1",
            name="write_file",
            arguments={"path": "f.txt", "content": "y"},
        )
    )
    assert not result.ok
    assert "not permitted" in result.content or "not allow" in result.content


def test_finish_task_raises(tmp_path: Path):
    execu = ToolExecutor(make_ctx(tmp_path))
    with pytest.raises(TaskFinished):
        execu.execute(ToolCall(id="1", name="finish_task", arguments={"summary": "done"}))


def test_denied_command_not_run(tmp_path: Path):
    execu = ToolExecutor(make_ctx(tmp_path))
    result = execu.execute(
        ToolCall(
            id="1",
            name="run_command",
            arguments={"command": "git push"},
        )
    )
    assert not result.ok
    assert "denied" in result.content


def test_run_command_executes(tmp_path: Path):
    """A command the profile auto-allows runs and returns its output.

    Uses ``development``: since v0.1.4 ``safe-edit`` auto-allows no generic command (spec §4.2), so
    this is now the profile where unattended execution is the expected behaviour.
    """

    execu = ToolExecutor(make_ctx(tmp_path, DEVELOPMENT))
    result = execu.execute(
        ToolCall(
            id="1",
            name="run_command",
            arguments={"command": "echo hello-openagent"},
        )
    )
    assert result.ok
    assert "hello-openagent" in result.content


def test_run_command_under_safe_edit_needs_approval_then_executes(tmp_path: Path):
    """Under safe-edit the same command is gated — and still works once a human agrees."""

    denied = ToolExecutor(make_ctx(tmp_path)).execute(
        ToolCall(id="1", name="run_command", arguments={"command": "echo hello-openagent"})
    )
    assert not denied.ok
    assert "not approved" in denied.content

    ctx = make_ctx(tmp_path)
    ctx.approval_gate = ApprovalGate(callback=lambda _request: True)
    approved = ToolExecutor(ctx).execute(
        ToolCall(id="2", name="run_command", arguments={"command": "echo hello-openagent"})
    )
    assert approved.ok
    assert "hello-openagent" in approved.content


def test_schemas_filtered_by_profile():
    read_only = {s["name"] for s in schemas_for_profile(get_profile(READ_ONLY))}
    assert "apply_patch" not in read_only
    assert "read_file" in read_only
    safe = {s["name"] for s in schemas_for_profile(get_profile(SAFE_EDIT))}
    assert "apply_patch" in safe
    assert "run_tests" in safe


def test_every_tool_schema_is_closed_to_unknown_properties():
    assert all(tool.parameters["additionalProperties"] is False for tool in ALL_TOOLS.values())
    run_tests = ALL_TOOLS["run_tests"].parameters
    assert run_tests["properties"]["argv"]["type"] == "array"
    assert "command" not in run_tests["properties"]


def test_executor_rejects_unknown_and_oversized_arguments(tmp_path: Path):
    (tmp_path / "file.txt").write_text("x")
    executor = ToolExecutor(make_ctx(tmp_path))
    unknown = executor.execute(
        ToolCall(
            id="extra",
            name="read_file",
            arguments={"path": "file.txt", "unexpected": True},
        )
    )
    assert not unknown.ok and "Additional properties" in unknown.content

    oversized = executor.execute(
        ToolCall(
            id="large",
            name="write_file",
            arguments={"path": "large.txt", "content": "x" * 70_000},
        )
    )
    assert not oversized.ok and "exceeds 65536 bytes" in oversized.content


def _replace_read_handler(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    original = ALL_TOOLS["read_file"]

    def _raise(_ctx, **_kwargs):
        raise exc

    monkeypatch.setitem(
        ALL_TOOLS,
        "read_file",
        Tool(original.name, original.description, original.parameters, _raise),
    )


@pytest.mark.parametrize(
    ("exc", "error_type"),
    [
        (OSError("disk unavailable"), "os_error"),
        (PermissionError("access denied"), "permission_denied"),
        (UnicodeError("invalid encoding"), "encoding_error"),
        (WorkspaceBudgetExceeded("workspace byte budget exceeded"), "workspace_budget_exceeded"),
        (ExecutionBackendError("backend unavailable"), "execution_backend_error"),
        (OutputLimitExceeded(10), "output_limit_exceeded"),
    ],
)
def test_executor_converts_operational_exceptions_to_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
    error_type: str,
):
    _replace_read_handler(monkeypatch, exc)
    result = ToolExecutor(make_ctx(tmp_path)).execute(
        ToolCall(id="1", name="read_file", arguments={"path": "anything"})
    )
    assert result.ok is False
    assert result.data["error_type"] == error_type
    assert result.content


def test_operational_exception_secret_is_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    secret = "prefixless-secret-99887766"
    _replace_read_handler(monkeypatch, OSError(f"backend echoed {secret}"))
    with secret_scope(secret):
        result = ToolExecutor(make_ctx(tmp_path)).execute(
            ToolCall(id="1", name="read_file", arguments={"path": "anything"})
        )
    assert result.ok is False
    assert secret not in result.content
    assert "[REDACTED]" in result.content


def test_unexpected_handler_exception_fails_with_normalized_internal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    secret = "sk-secret-value-that-must-not-escape"
    _replace_read_handler(monkeypatch, AssertionError(f"invariant broke: {secret}"))
    with pytest.raises(ToolExecutionInternalError) as excinfo:
        ToolExecutor(make_ctx(tmp_path)).execute(
            ToolCall(id="1", name="read_file", arguments={"path": "anything"})
        )
    assert excinfo.value.error_type == "tool_internal_error"
    assert secret not in str(excinfo.value)
    assert "internal error" in str(excinfo.value)


@pytest.mark.parametrize(
    "exc",
    [
        KeyboardInterrupt(),
        SystemExit(2),
        asyncio.CancelledError(),
        RunCancelled(),
    ],
)
def test_executor_never_swallows_process_or_cancellation_control_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: BaseException
):
    _replace_read_handler(monkeypatch, exc)
    with pytest.raises(type(exc)):
        ToolExecutor(make_ctx(tmp_path)).execute(
            ToolCall(id="1", name="read_file", arguments={"path": "anything"})
        )
