# Multi-agent demo: building the weather map app

This is the end-to-end demonstration that OpenAgent v0.1 can create real agents, dispatch real
tasks to them, track their runs, and drive a real deliverable to completion — including a review and
revision round. Everything here was produced through OpenAgent's own CLI (`openagent add` /
`openagent run` / `openagent output`), driving the real **`agy`** (Google Antigravity) CLI.

- **Orchestrator / supervisor:** the Claude Code session that built OpenAgent.
- **Worker agents:** three `agy` agents, created and run **through OpenAgent**.
- **Deliverable:** [`examples/weather-map-app`](../examples/weather-map-app/) — a working,
  no-API-key weather map (verified live in a browser).

No secrets or credentials appear in any artifact below.

## Agents created (via `openagent add … --cli antigravity`)

| Agent | Role | Backend | agy model |
| --- | --- | --- | --- |
| `weather-data-agent` | Weather data-source + API design | antigravity (`agy`) | agy default (read-only / plan) |
| `weather-ui-agent` | Map UI / UX + interaction design | antigravity (`agy`) | agy default (read-only / plan) |
| `weather-qa-agent` | Code review (correctness / a11y / UX) | antigravity (`agy`) | agy default (read-only / plan) |

Model selection: `agy` exposes 8 models via `agy models` (the new CLI model-discovery step —
Gemini 3.x, Claude Sonnet/Opus 4.6, GPT-OSS). These runs used `agy`'s configured default in
read-only (`--mode plan`) — see "Real limitations" for why the agents ran read-only.

## Runs (real run IDs + terminal states)

| Run ID | Agent | Task | Terminal state |
| --- | --- | --- | --- |
| `run_c9d4522fb880` | weather-data-agent | Free, no-key weather data sources | ✅ completed |
| `run_8d5f390dc20e` | weather-data-agent | Exact Open-Meteo request URLs + fields | ✅ completed |
| `run_bdbbb6891772` | weather-ui-agent | UI/layout + interaction spec | ✅ completed |
| `run_f9e7471cd38f` | weather-qa-agent | Full 3-file review | ❌ failed (`antigravity_error: timeout waiting for response`) |
| `run_1fd3fc958909` | weather-qa-agent | Focused review of `app.js` | ✅ completed |

The failed QA run is shown honestly: a plan-mode review of all three files exceeded agy's print
timeout, so — with no `SUCCESS` object — OpenAgent's fail-closed reconciliation correctly recorded
`run.failed`, **not** a fabricated success. A tighter, single-file review (`run_1fd3fc958909`)
completed and produced the finding below.

## What each agent produced

**`weather-data-agent`** returned the exact Open-Meteo endpoints and JSON fields the app uses:

- Geocoding: `https://geocoding-api.open-meteo.com/v1/search?name=…` → `results[0].latitude/longitude`.
- Forecast: `https://api.open-meteo.com/v1/forecast?…&current=…&hourly=…&daily=…` with
  `temperature_2m`, `apparent_temperature`, `relative_humidity_2m`, `precipitation`,
  `precipitation_probability`, `weather_code`, `wind_speed_10m`, `wind_direction_10m`,
  `temperature_2m_min/max`.

**`weather-ui-agent`** returned the UI spec the app implements: full-screen Leaflet + OSM map, a
glassmorphic floating panel (desktop) / swipeable bottom sheet (mobile), map-click → reverse-geocode
→ weather, debounced city search, a "locate me" control, a °C/°F toggle, and a temperature legend.

## Review → revision round (the required loop)

**`weather-qa-agent` finding (`run_1fd3fc958909`), verbatim intent:**

> **Bug:** the "Retry" button is broken on initial-load failure and retries the *wrong* location on
> subsequent failures, because `last` is only updated after a *successful* fetch.
> **Fix:** set `last = { lat, lon, name }` at the **start** of `select`, before the request.

This was a **real defect** in the app. The revision applied it — `last` is now recorded before the
request in [`app.js`](../examples/weather-map-app/app.js) `select()`, and the °C/°F toggle guards on
`last.data`.

**Supervisor review (this session)** added one more real correctness fix: Open-Meteo returns
location-local timestamps (`timezone=auto`) with no offset; parsing them with `new Date(t)` used the
*viewer's* timezone, drifting the "now" alignment and the shown hour. Fixed to compare in the
location's own clock via `utc_offset_seconds` (and daily weekdays parsed as UTC).

### Revisions verified live (browser)

- **Retry fix:** a fresh selection of *Reykjavik* was forced to fail → the error state showed; after
  restoring the network and clicking **Retry**, the app recovered **Reykjavik** (not the previously
  loaded Tokyo). Before the fix it would have retried Tokyo.
- **Timezone fix:** for *Tokyo* (UTC+9) the hourly strip starts at the correct local hour (18:00 when
  the local wall clock is 17:xx), independent of the viewer's timezone.
- **Happy path:** Istanbul / Tokyo / Reykjavik all load current conditions, feels-like, precip
  amount + probability, humidity, wind speed + direction, a 24-hour strip, and a 7-day min/max
  forecast, with a map marker + popup.

## Run the app

```bash
cd examples/weather-map-app
python3 -m http.server 8777
# open http://localhost:8777
```

## Reproduce the orchestration

```bash
openagent add --name weather-data-agent --cli antigravity --profile read-only \
  --title "Weather data agent" --description "Weather data-source design"
openagent add --name weather-ui-agent   --cli antigravity --profile read-only \
  --title "Weather UI agent"   --description "Map UI/UX design"
openagent add --name weather-qa-agent   --cli antigravity --profile read-only \
  --title "Weather QA agent"   --description "Code review"

openagent run --name weather-data-agent --worktree none -p "<data-source task>"
openagent run --name weather-ui-agent   --worktree none -p "<ui-design task>"
# …integrate outputs into examples/weather-map-app…
openagent run --name weather-qa-agent   --worktree none -p "Review examples/weather-map-app/app.js …"
# …apply the QA finding, re-verify…

openagent runs                       # see every run + terminal state
openagent output --id <run-id> --format json   # a run's result (valid JSON, machine-readable)
```

## Real limitations (honest)

- **Agents ran read-only (`--mode plan`).** For `agy` to *edit files* non-interactively it needs
  `--dangerously-skip-permissions`, which disables Antigravity's own permission checks while
  OpenAgent cannot observe its internal tool calls. OpenAgent treats that as **experimental, opt-in**
  (`OPENAGENT_ANTIGRAVITY_EXPERIMENTAL_EDIT`) and never infers it from a "safe-edit" profile. In this
  demo the worker agents therefore produced **designs and a real review**, and the supervisor
  integrated their output into the files; the QA agent's finding drove a real, verified code change.
  Letting the agents write the files directly is possible but requires enabling that bypass
  deliberately (and, in a sandboxed automation context, a human-approved permission step).
- **agy plan-mode reviews can exceed the print timeout** on multi-file prompts (see the failed QA
  run). Keep review prompts tightly scoped, or raise the timeout.
