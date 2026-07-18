# OpenAgent

This repository uses OpenAgent to discover and run external AI agents.

## Instructions for AI Assistants

1. Run `openagent list --json` to discover available agents.
2. Delegate work with:
   `openagent run --name <name> --prompt "<task>" --worktree auto`
3. Retrieve a result with:
   `openagent output --id <run-id> --format json`
4. Never request or expose credentials.
5. Use isolated worktrees for file-changing tasks.

## Available Agents

<!-- OPENAGENT:AGENTS:START -->

### GLM_Coder

- Name: `GLM`
- Runtime: `api`
- Tags: `coder`, `writer`
- Description: write code and testing

### gpt56sol

- Name: `Sol`
- Runtime: `codex-cli`
- Tags: `Code py go`
- Description: the best coder

### Tester agent

- Name: `agy-tester`
- Runtime: `antigravity-cli`
- Tags: `tester`
- Description: Go tester agent

### Weather data agent

- Name: `weather-data-agent`
- Runtime: `antigravity-cli`
- Tags: `weather`, `data`
- Description: Analyzes weather data-source options for a map app

### Weather QA agent

- Name: `weather-qa-agent`
- Runtime: `antigravity-cli`
- Tags: `weather`, `qa`
- Description: Reviews the weather app for correctness, UX and accessibility

### Weather UI agent

- Name: `weather-ui-agent`
- Runtime: `antigravity-cli`
- Tags: `weather`, `ui`
- Description: Designs the map UI/UX for the weather app

<!-- OPENAGENT:AGENTS:END -->

