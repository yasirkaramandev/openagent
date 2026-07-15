# OpenAgent skills

This folder holds **skills** — task-focused guides that teach an AI assistant how to use OpenAgent
correctly and safely. A skill is documentation, not an executable: nothing here runs on its own. It
is meant to be handed to an AI agent (as context, a system prompt, or a retrieved document) so the
agent drives the real `openagent` CLI the way it is designed to be driven.

## Available skills

| Skill | What it covers |
| --- | --- |
| [`openagent`](openagent/SKILL.md) | Agent/provider setup, model selection, running tasks, inspecting artifacts, cancellation, resume, and the security boundaries an AI must respect. |

## How to give a skill to an AI agent

The skill file is plain Markdown with YAML front matter (`name`, `description`). Provide its contents
to whichever assistant you are using:

- **Claude Code** — reference or paste `skills/openagent/SKILL.md` into the session, or place it where
  your project loads skills/instructions from.
- **Codex** and other agents — include the file as context (a system/developer message, a retrieved
  document, or an attached file).

The assistant then follows the mandatory start-up flow (`openagent version` → `doctor --json` →
`discover` → `agent list --json` → `provider list --json`) and the security rules the skill spells
out.

## Keeping skills accurate

A skill must match the **real** CLI behavior. Every command in [`openagent/SKILL.md`](openagent/SKILL.md)
is accepted by the current `openagent` CLI. If the CLI changes (new flags, renamed commands), update
the skill in the same change so it never teaches a command the CLI no longer accepts. Validate quickly
with:

```bash
openagent --help
openagent add --help
openagent run --help
openagent output --help
```
