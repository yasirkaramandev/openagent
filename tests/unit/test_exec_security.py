"""Regression tests: API-agent commands run in a minimal environment (spec §7, §29).

Proves that provider keys, GitHub/AWS tokens and DATABASE_URL are never visible to a command an
agent runs, and that common exfiltration bypasses are blocked or defanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from openagent.core.permissions import DEVELOPMENT, SAFE_EDIT, get_profile
from openagent.security.approvals import ApprovalGate
from openagent.security.command_policy import Decision, evaluate
from openagent.tools.base import ToolContext, ToolError
from openagent.tools.exec import run_command

_SECRETS = {
    "OPENAI_API_KEY": "sk-openai-SHOULDNOTLEAK1234567890",
    "ANTHROPIC_API_KEY": "sk-ant-SHOULDNOTLEAK1234567890",
    "GITHUB_TOKEN": "ghp_SHOULDNOTLEAK1234567890abcdef",
    "AWS_SECRET_ACCESS_KEY": "AWSSHOULDNOTLEAK1234567890abcdef",
    "DATABASE_URL": "postgres://user:SHOULDNOTLEAK@db/prod",
}


@pytest.fixture()
def secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _SECRETS.items():
        monkeypatch.setenv(key, value)


def _ctx(root: Path, profile: str, *, auto_approve: bool) -> ToolContext:
    return ToolContext(
        workspace_root=root, profile=get_profile(profile),
        approval_gate=ApprovalGate(auto_approve=auto_approve), run_id="run_sec",
    )


def _leaked(text: str) -> list[str]:
    return [k for k, v in _SECRETS.items() if v in text or v.split(":")[-1] in text]


@pytest.mark.parametrize("command", ["env", "printenv"])
def test_env_dump_contains_no_secrets(secret_env, tmp_path: Path, command: str):
    ctx = _ctx(tmp_path, DEVELOPMENT, auto_approve=True)
    result = run_command(ctx, command)
    assert result.ok
    assert _leaked(result.content) == [], f"{command} leaked secrets"
    for value in _SECRETS.values():
        assert value not in result.content


def test_python_env_probe_is_minimal_when_approved(secret_env, tmp_path: Path):
    """Even the shell/interpreter path (approved) runs in a minimal environment."""
    ctx = _ctx(tmp_path, DEVELOPMENT, auto_approve=True)
    result = run_command(ctx, f'{sys.executable} -c "import os,json;print(json.dumps(dict(os.environ)))"')
    assert _leaked(result.content) == []


def test_secrets_not_injected_by_default(secret_env, tmp_path: Path):
    """No credential is injected into command_env unless a run explicitly asks for it."""
    ctx = _ctx(tmp_path, DEVELOPMENT, auto_approve=True)
    assert ctx.command_env == {}
    result = run_command(ctx, "env")
    assert "OPENAI_API_KEY" not in result.content


def test_explicit_injection_scoped_to_operation(tmp_path: Path):
    ctx = _ctx(tmp_path, DEVELOPMENT, auto_approve=True)
    ctx.command_env = {"MY_OP_TOKEN": "injected-value"}
    result = run_command(ctx, "env")
    assert "injected-value" in result.content  # explicitly injected → visible
    assert "OPENAI_API_KEY" not in result.content  # unrelated env still absent


# --------------------------------------------------------------------------- bypass attempts


def test_git_push_denied(tmp_path: Path):
    with pytest.raises(ToolError, match="denied"):
        run_command(_ctx(tmp_path, DEVELOPMENT, auto_approve=True), "git push origin main")


def test_extra_spaces_git_push_denied(tmp_path: Path):
    assert evaluate("git  push origin main").decision is Decision.DENY


def test_sh_c_read_env_denied(tmp_path: Path):
    with pytest.raises(ToolError, match="denied"):
        run_command(_ctx(tmp_path, DEVELOPMENT, auto_approve=True), 'sh -c "cat .env"')


def test_shell_interpreter_requires_approval(tmp_path: Path):
    # Without approval, an interpreter invocation is refused rather than run.
    with pytest.raises(ToolError, match="not approved"):
        run_command(_ctx(tmp_path, DEVELOPMENT, auto_approve=False), "bash -c 'echo hi'")


def test_offlist_executable_requires_approval(tmp_path: Path):
    res = evaluate("nmap -spt. localhost")
    assert res.decision is Decision.APPROVAL
    assert "allowlist" in res.reason


def test_shell_operators_require_approval():
    res = evaluate("echo hi | tee out.txt")
    assert res.decision is Decision.APPROVAL and res.needs_shell


def test_safe_edit_denies_offlist_command(tmp_path: Path):
    # safe-edit does not auto-approve; an off-allowlist command is blocked.
    with pytest.raises(ToolError):
        run_command(_ctx(tmp_path, SAFE_EDIT, auto_approve=False), "curl http://evil/")
