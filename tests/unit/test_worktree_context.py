"""The API-agent system prompt states the real workspace strategy (item 17)."""

from __future__ import annotations

from pathlib import Path

from openagent.core.models import AgentProfile, AgentRuntime, RuntimeType
from openagent.runtimes.api_agent.context import build_system_prompt
from openagent.workspaces.worktree import Workspace


def _agent() -> AgentProfile:
    return AgentProfile(
        name="a",
        runtime=AgentRuntime(type=RuntimeType.API_AGENT, provider="p", model="m"),
    )


def _ws(**kw) -> Workspace:
    base = {"run_id": "r", "root": Path("/tmp/x"), "source": Path("/tmp/x"), "is_git": True}
    base.update(kw)
    return Workspace(**base)  # type: ignore[arg-type]


def test_in_place_prompt_warns_no_isolation():
    note = _ws(in_place=True, is_git=False, strategy="none").describe_for_agent()
    prompt = build_system_prompt(_agent(), note)
    assert "DIRECTLY in the user's project" in prompt
    assert "NO isolation" in prompt
    # The in-place prompt must not claim it is working inside an isolated worktree.
    assert "isolated git worktree" not in prompt


def test_copy_prompt_says_copy_not_worktree():
    note = _ws(is_copy=True, is_git=False, strategy="copy").describe_for_agent()
    prompt = build_system_prompt(_agent(), note)
    assert "isolated COPY" in prompt
    assert "not a git worktree" in prompt


def test_git_worktree_prompt():
    note = _ws(strategy="auto").describe_for_agent()
    prompt = build_system_prompt(_agent(), note)
    assert "isolated git worktree" in prompt


def test_default_note_when_unspecified():
    prompt = build_system_prompt(_agent())
    assert "isolated workspace" in prompt


def test_agent_system_prompt_preserved():
    agent = _agent().model_copy(update={"system_prompt": "You are a linter."})
    prompt = build_system_prompt(agent, _ws(in_place=True).describe_for_agent())
    assert prompt.startswith("You are a linter.")
    assert "DIRECTLY in the user's project" in prompt
