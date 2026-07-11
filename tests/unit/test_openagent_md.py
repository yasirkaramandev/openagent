from pathlib import Path

from openagent.config import OPENAGENT_MD_END, OPENAGENT_MD_START
from openagent.core.models import AgentProfile, AgentRuntime, RuntimeType
from openagent.reporting.openagent_md import render_agents_block, write_openagent_md


def _agent(name: str) -> AgentProfile:
    return AgentProfile(
        name=name, title=f"{name} title",
        runtime=AgentRuntime(type=RuntimeType.CLI, cli="codex"),
        tags=["coder"], description="does things",
    )


def test_render_block_has_markers_and_agent():
    block = render_agents_block([_agent("codex-coder")])
    assert OPENAGENT_MD_START in block and OPENAGENT_MD_END in block
    assert "`codex-coder`" in block
    assert "codex-cli" in block


def test_write_creates_file(tmp_path: Path):
    path = tmp_path / "OPENAGENT.md"
    write_openagent_md(path, [_agent("a")])
    text = path.read_text()
    assert "# OpenAgent" in text
    assert "`a`" in text


def test_write_preserves_prose_outside_markers(tmp_path: Path):
    path = tmp_path / "OPENAGENT.md"
    write_openagent_md(path, [_agent("a")])
    # Add custom prose before and after the managed block.
    text = path.read_text()
    text = "# My custom heading\n\n" + text + "\n## Custom footer\n"
    path.write_text(text)
    # Regenerate with a different agent set.
    write_openagent_md(path, [_agent("b")])
    updated = path.read_text()
    assert "# My custom heading" in updated
    assert "## Custom footer" in updated
    assert "`b`" in updated
    assert "`a`" not in updated
