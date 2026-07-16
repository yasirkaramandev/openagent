from pathlib import Path

import pytest

from openagent.core.events import ToolCall
from openagent.core.permissions import READ_ONLY, SAFE_EDIT, get_profile
from openagent.security.approvals import ApprovalGate
from openagent.tools.base import ToolContext, ToolError
from openagent.tools.control import TaskFinished
from openagent.tools.registry import ToolExecutor, schemas_for_profile


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
    execu = ToolExecutor(make_ctx(tmp_path))
    result = execu.execute(
        ToolCall(
            id="1",
            name="run_command",
            arguments={"command": "echo hello-openagent"},
        )
    )
    assert result.ok
    assert "hello-openagent" in result.content


def test_schemas_filtered_by_profile():
    read_only = {s["name"] for s in schemas_for_profile(get_profile(READ_ONLY))}
    assert "apply_patch" not in read_only
    assert "read_file" in read_only
    safe = {s["name"] for s in schemas_for_profile(get_profile(SAFE_EDIT))}
    assert "apply_patch" in safe
    assert "run_tests" in safe
