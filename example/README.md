# OpenAgent Task Board Example

This is the working OpenAgent v0.2 reference project: a small Python task board
that an agent can inspect, safely edit, test, cancel, and resume. The application
has no runtime dependency outside the standard library, persists validated JSON
atomically, and never needs a credential or network connection.

The committed `app/` is the **fixed** stage and all committed tests pass. A setup
script creates a disposable **buggy** stage under `.demo/` where completing an
already-completed task increments `completed_count` a second time. The training
prompts lead an OpenAgent run through diagnosis and a same-run resume without
leaving broken code on the repository branch.

## Layout

```text
app/                 task model, JSON repository, service, and CLI
tests/               fully offline repository/service/CLI tests
prompts/             inspect, feature, controlled-bug, and resume turns
expected/            sample Doctor, event, and resume artifacts plus schemas
scenarios/           fixed-stage explanation and the controlled buggy overlay
scripts/             cross-platform setup, run, resume, and verification tools
OPENAGENT.md          hard workspace, shell, network, and secret policy
```

## 1. Install the example dependencies

Python 3.10 or newer is required. The app itself needs no package. Pytest is the
only test dependency. These are one-time environment setup commands; unlike the
tests and verifier, package installation may consult your configured package
index when the dependency is not cached.

POSIX (Linux/macOS):

```sh
cd example
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
```

PowerShell:

```powershell
Set-Location example
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
```

When working from an OpenAgent source checkout, use the repository development
environment instead; it already contains pytest and OpenAgent's dependencies.

## 2. Run the committed, fixed tests

```sh
python -m pytest -q -p no:cacheprovider
python -m app.cli --db .demo-board.json add "Review OpenAgent" --priority high
python -m app.cli --db .demo-board.json list
python -m app.cli --db .demo-board.json complete 1
python -m app.cli --db .demo-board.json summary
```

The second `complete 1` is intentionally safe: it reports `already completed`
and leaves the counter at one. `.demo-board.json` is disposable local data.

## 3. Create the controlled buggy workspace

The setup never edits committed code and refuses destinations outside this
`example/` directory. `--force` works only on a directory carrying the setup
script's marker.

POSIX:

```sh
./scripts/setup_example.sh --scenario buggy --destination .demo/buggy
cd .demo/buggy
python -m pytest -q -p no:cacheprovider
```

PowerShell:

```powershell
.\scripts\setup_example.ps1 --scenario buggy --destination .demo/buggy
Set-Location .demo/buggy
python -m pytest -q -p no:cacheprovider
```

Exactly the repeated-completion regression is expected to fail in this training
copy. Use `--scenario fixed` to generate a second copy whose full suite passes.
The verifier exercises both stages automatically.

## 4. Initialize OpenAgent and run Doctor

Run these commands from the generated `.demo/buggy` workspace:

```sh
openagent init
openagent doctor --json
openagent discover
openagent cli list --json
```

`doctor --json` is diagnostic and may return a nonzero status when an optional
CLI/provider is unavailable; read its `exit_code` and checks instead of masking
it. `expected/doctor.json.example` documents the example-facing artifact shape.

To use the TUI, run the real top-level command:

```sh
openagent
```

In the TUI, open **Add Agent**, choose **CLI** or **API**, select the runtime or
provider below, keep the `safe-edit` profile, review the workspace disclosure,
and create the agent. Runs, cancellation, resume, Doctor, and artifacts are also
available from their corresponding TUI screens.

## 5. Configure example agents

All commands below match the current `openagent --help` command shapes. They do
not put credentials on the command line.

### Codex CLI agent

Requires an installed/authenticated Codex CLI:

```sh
openagent agent add --name task-board-codex --cli codex --profile safe-edit
```

### Claude Code agent

Requires an installed/authenticated Claude Code CLI:

```sh
openagent agent add --name task-board-claude --cli claude --profile safe-edit
```

### Ollama local API agent

Requires a locally running Ollama server and a model already installed. It does
not require a credential and OpenAgent does not download a model for you.

```sh
openagent provider add ollama-local --type ollama --no-key
openagent provider probe ollama-local --model llama3.2 --json
openagent agent add --name task-board-ollama --provider ollama-local --model llama3.2 --profile safe-edit
```

### Gemini cloud placeholder

This requires your own credential. Set `GEMINI_API_KEY` in your private shell or
credential manager; never write its value in this repository or a command.

```sh
openagent provider add gemini-cloud --type gemini --key-env GEMINI_API_KEY
openagent provider probe gemini-cloud --model gemini-2.5-flash --json
openagent agent add --name task-board-gemini --provider gemini-cloud --model gemini-2.5-flash --profile safe-edit
```

OpenRouter is an equivalent cloud placeholder and also requires your own
credential:

```sh
openagent provider add openrouter-cloud --type openrouter --key-env OPENROUTER_API_KEY
openagent provider probe openrouter-cloud --model "YOUR_PUBLISHER/YOUR_MODEL" --json
openagent agent add --name task-board-openrouter --provider openrouter-cloud --model "YOUR_PUBLISHER/YOUR_MODEL" --profile safe-edit
```

Use only an agent that Doctor reports as usable. The offline example verifier
does not invoke any agent or provider and does not require these credentials.

## 6. Ten-step CLI demo

The examples below use `task-board-codex`; substitute another configured agent
name if needed. From `.demo/buggy`, follow this exact sequence.

1. Install dependencies as described above.
2. Run `python -m pytest -q -p no:cacheprovider` and observe the one controlled
   failure in the buggy copy.
3. Run `openagent doctor --json` and resolve blocker-level findings.
4. Create an agent using one of the exact commands above (or the TUI).
5. Start the read-only inspect run.

   POSIX:

   ```sh
   openagent run --name task-board-codex --prompt "$(cat prompts/01_inspect.md)" --worktree auto
   ```

   PowerShell:

   ```powershell
   openagent run --name task-board-codex --prompt (Get-Content prompts/01_inspect.md -Raw) --worktree auto
   ```

6. Start the controlled bug diagnosis run in terminal A.

   POSIX:

   ```sh
   openagent run --name task-board-codex --prompt "$(cat prompts/03_fix_bug.md)" --worktree auto
   ```

   PowerShell:

   ```powershell
   openagent run --name task-board-codex --prompt (Get-Content prompts/03_fix_bug.md -Raw) --worktree auto
   ```

7. While terminal A is active, get its id and cancel it from terminal B.

   ```text
   openagent runs --limit 5 --json
   openagent cancel --id RUN_ID
   ```

8. Resume the **same** run for the fix.

   POSIX:

   ```sh
   openagent resume --id "$RUN_ID" --prompt "$(cat prompts/04_resume.md)"
   ```

   PowerShell:

   ```powershell
   openagent resume --id $RunId --prompt (Get-Content prompts/04_resume.md -Raw)
   ```

9. Inspect durable artifacts without guessing their paths.

   ```text
   openagent output --id RUN_ID --format json
   openagent output --id RUN_ID --format events
   openagent output --id RUN_ID --format diff
   openagent output --id RUN_ID --format tests
   ```

10. In the run's owned workspace, run
    `python -m pytest -q -p no:cacheprovider` and confirm the repeated completion
    test and the full suite pass.

The first line of `openagent run` prints the run id. Because an especially fast
runtime may finish before terminal B sends cancellation, use a new diagnosis run
when demonstrating cancellation; never pretend a terminal run was cancelled.

## 7. Same-run resume: CLI and application API

The two turns are deliberately separate:

- Turn 1, `prompts/03_fix_bug.md`: reproduce and diagnose; do not edit.
- Turn 2, `prompts/04_resume.md`: resume the same run, fix, and test.

The CLI route is:

```text
openagent resume --id RUN_ID --prompt "..."
```

The application-service API route calls the same `RunService.resume` lifecycle
directly. It is executable, not pseudocode:

```sh
python scripts/resume_via_api.py --id "$RUN_ID" --prompt-file prompts/04_resume.md --project-root .
```

PowerShell:

```powershell
python scripts/resume_via_api.py --id $RunId --prompt-file prompts/04_resume.md --project-root .
```

Internally the helper creates `OpenAgentApp` for the named project and awaits
`application.runs.resume(run_id, prompt)`. The current credential is resolved by
OpenAgent at execution time; the helper never accepts a credential. CLI-native
sessions and provider/API continuations remain bound to their recorded runtime,
provider, protocol, model, project, and integrity metadata.

## 8. Security and offline verification

Run the cross-platform verifier with the Python environment that contains both
pytest and OpenAgent:

```sh
python scripts/verify_example.py
```

Or use the wrappers:

```sh
./scripts/run_example.sh
```

```powershell
.\scripts\run_example.ps1
```

The verifier uses only Python subprocesses, supplies a sanitized environment,
denies socket connections and shell execution in child processes, denies file
writes outside the example and its private temporary directory, and never runs
an installer. It checks:

- required files and `OPENAGENT.md` policy;
- imports and the full offline pytest suite;
- Task Board CLI JSON behavior;
- side-effect-free OpenAgent `--help` smoke for run/cancel/resume/output/Doctor;
- parsing and schema validation for every expected JSON/JSONL artifact;
- the fixed scenario passes while the generated buggy scenario exposes the
  counter regression;
- no inherited secret value appears in a child environment or output;
- no network operation or forbidden shell command succeeds;
- a before/after hash snapshot shows no repository file outside `example/`
  changed.

Success ends with `EXAMPLE VERIFICATION PASSED`. No provider, CLI agent, network,
or secret is needed for this verification.

