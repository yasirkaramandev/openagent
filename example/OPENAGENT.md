# OpenAgent Task Board Example — Agent Contract

## Project purpose

Analyze, safely improve, and test a small offline Python task board. The
application stores tasks in a local JSON file and demonstrates a two-turn
inspect/resume workflow without sending project data over the network.

## Workspace boundary

- Do not modify files outside `/example`.
- In a generated training workspace, edit only `app/**/*.py` and `tests/**/*.py`
  unless the prompt explicitly names another file under that workspace.
- You may read `README.md`, `prompts/`, `expected/`, and this file.
- Do not modify `scenarios/`, `scripts/`, `expected/`, parent directories,
  repository configuration, CI workflows, or OpenAgent's source tree.
- Do not follow symbolic links out of the workspace.

## Required checks

Run from the example workspace before finishing:

```text
python -m pytest -q -p no:cacheprovider
python -m compileall -q app
```

When Ruff is available, formatting checks are:

```text
python -m ruff format --check app tests
python -m ruff check app tests
```

Use `python -m ruff format app tests` only when a prompt authorizes formatting.

## Shell policy

- Allowed: the Python, pytest, Ruff, `git status`, and `git diff` commands above.
- Do not access the network or invoke package installers during an agent run.
- Do not invoke `curl`, `wget`, SSH, remote Git operations, shells with
  downloaded input, privilege escalation, or destructive commands.
- Do not run `rm -rf`, `git reset`, `git clean`, or commands outside this
  workspace.

## Secret policy

- Do not read, enumerate, print, copy, or modify environment secrets, keychains,
  credential files, browser state, SSH configuration, or cloud configuration.
- The app requires no credential. Never add a credential to code, prompts,
  fixtures, command arguments, logs, or artifacts.
- Provider examples in the README name environment variables only; their values
  belong to the user and are not part of this project.

## Success criteria

- The requested behavior is covered by an offline regression test.
- Repeated completion is idempotent: an already-completed task does not change
  `completed_count`.
- Invalid titles, priorities, ids, and corrupt JSON fail clearly.
- All tests pass and imports compile.
- No network was used, no secret was read, and no file outside `/example` was
  changed.

