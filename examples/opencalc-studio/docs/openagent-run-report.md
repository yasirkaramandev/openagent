# OpenCalc Studio — OpenAgent build run report

**Operator-compiled provenance.** This report was compiled by the OpenAgent orchestration operator
from real run records. Every _product_ and _plan_ file in this example was authored by an OpenAgent
agent run (see the table); none was hand-written. The operator's role was limited to: creating the
agent team, writing task prompts, launching runs, verifying outputs (running `vitest`), integrating
agent-authored files, and recording provenance.

## Environment

- Branch: `example/opencalc-studio`, based on the verified rc4 head `f5abf266…`.
- Provider: single configured `z-ai/glm-5.2` (NVIDIA Build, `openai-chat`). No other provider was
  available (`claude`/`gemini` not installed, `codex` usage-limited).
- Agents (all `api-agent`, provider `z-ai/glm-5.2`, `full-access`, created for this build):
  `glm-orchestrator`, `agy-frontend`, `agy-qa`, `agy-security`, `agy-docs`.
- Toolchain: Node 22, npm 10 (present); Docker absent.

## Runs

| Run ID             | Agent            | Status                          | Outcome                                                                               |
| ------------------ | ---------------- | ------------------------------- | ------------------------------------------------------------------------------------- |
| `run_3bc9d9d80d23` | glm-orchestrator | completed                       | Plan (task graph + architecture + acceptance) — integrated                            |
| `run_e38ed3f90b82` | agy-frontend     | failed (429 after deliverables) | Calc engine + scaffold + 44-test suite — salvaged & integrated; **43/44 vitest pass** |
| `run_af50a11af338` | agy-frontend     | failed (429)                    | parser fix — blocked by quota                                                         |
| `run_7b918783984c` | agy-frontend     | failed (429)                    | parser fix retry — blocked by quota                                                   |
| `run_6be865059cc0` | glm-orchestrator | completed-empty                 | premature text-only turn ended the run before writing files                           |
| `run_29bcb1abfc89` | glm-orchestrator | failed                          | `write_file` content passed as object; stream disconnect                              |
| `run_49c06bcf1152` | glm-orchestrator | failed                          | stream >120s (before the timeout was raised)                                          |
| `run_28b3ab86d62e` | glm-orchestrator | failed                          | stream >120s (before the timeout was raised)                                          |

## What was delivered (agent-authored)

- **Plan** (`docs/`): `openagent-task-graph.json` (9 tasks), `architecture.md` (engine contract),
  `acceptance-criteria.md`.
- **Calculation engine** (`src/calculator/`): `types.ts`, `decimal.ts` (exact `0.1+0.2=0.3`),
  `tokenizer.ts`, `parser.ts`, `scientific.ts` (DEG/RAD), `evaluator.ts`, `index.ts`, plus
  `engine.test.ts` (44 cases) and the Vite/TypeScript/Vitest scaffold (`package.json`,
  `tsconfig.json`, `vitest.config.ts`).
- **Operator verification** (files unmodified): `npm install && npx vitest run` → **43 passed,
  1 failed**. The golden matrix passes: `0.1+0.2=0.3`, `2+3×4=14`, `(2+3)×4=20`, `sqrt(144)=12`,
  `5!=120`, `2^10=1024`, `sin(30° DEG)=0.5`, `1/0`→DivisionByZero, `sqrt(-1)`→DomainError.
- **Known issue** (agent's own inconsistency, not operator-fixed): its `engine.test.ts` expects a
  trailing `)` to throw `MismatchedParens`, but its `parser.ts` throws `SyntaxError`. The fix is an
  agy-frontend run; both attempts hit the provider quota (429).

## What was NOT completed and why

The remaining task-graph items — React UI, scientific-mode UI, history/settings/theme/PWA, expanded
QA (Playwright/axe), security review, docs, integration review, CI — were **not built**. Causes,
observed empirically:

1. **Provider slowness** — GLM turns routinely exceeded OpenAgent's 120s per-turn stream cap. (The
   operator temporarily raised it to 600s with user approval, then restored it.)
2. **Tool-argument unreliability** — GLM frequently passed `write_file.content` as an object instead
   of a string, and sometimes ended runs with a narration-only turn. Forceful "tool-first, content
   must be a string" prompting mitigated but did not eliminate this.
3. **Quota exhaustion** — after ~8 runs the provider returned persistent HTTP 429, blocking all
   further runs (including the small parser fix).

The work is **resumable**: the branch, agent team, and plan are in place. Resuming requires the GLM
quota to reset (and, for large turns, the per-turn stream timeout raised again).
