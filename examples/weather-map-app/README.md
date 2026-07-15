# Weather Map — OpenAgent example

An interactive, single-page weather map. Click anywhere on the map, search for a city, or use your
current location to see **live** current conditions, a 24-hour strip, and a 7-day forecast. No account,
no API key, no build step.

## Run it

It's a static site — serve the folder with any static file server:

```bash
cd examples/weather-map-app
python3 -m http.server 8777
# open http://localhost:8777
```

(Opening `index.html` directly via `file://` also works because all APIs send permissive CORS
headers, but a local server is closer to how you'd deploy it.)

## Features

- Full-screen [Leaflet](https://leafletjs.com/) map with OpenStreetMap tiles (whole world).
- Click any point → marker + popup + full weather in the side panel.
- City / place search with debounced autocomplete and keyboard navigation.
- "Use my location" via the browser Geolocation API.
- Current temperature, **feels-like**, precipitation (amount + probability), humidity, wind speed &
  compass direction, daily min/max, a 24-hour hourly strip, and a 7-day forecast.
- °C / °F toggle and a temperature colour legend.
- Loading, empty, and error states (with retry).
- Responsive: floating glass panel on desktop, swipeable bottom sheet on mobile.

## Data sources (all keyless)

| Purpose | Service | Endpoint |
| --- | --- | --- |
| Forecast (current / hourly / daily) | [Open-Meteo](https://open-meteo.com/) | `https://api.open-meteo.com/v1/forecast` |
| City search (geocoding) | [Open-Meteo Geocoding](https://open-meteo.com/en/docs/geocoding-api) | `https://geocoding-api.open-meteo.com/v1/search` |
| Map-click place names (reverse geocoding) | [Nominatim](https://nominatim.org/) (OpenStreetMap) | `https://nominatim.openstreetmap.org/reverse` |
| Map tiles | [OpenStreetMap](https://www.openstreetmap.org/) | tile server |

Weather codes are [WMO codes](https://open-meteo.com/en/docs) mapped to icons/labels in `app.js`.

## Privacy

Your location (whether from geolocation or a map click) is used **only** to centre the map and fetch
weather. It is never stored (no cookies, no `localStorage`) and never sent anywhere except the keyless
weather/geocoding services listed above.

## How this example was built

This app was produced as the multi-agent demonstration for OpenAgent v0.1. Three `agy`
(Google Antigravity) agents were created and run **through OpenAgent itself** (`openagent add` /
`openagent run`), with this Claude session acting as the orchestrator/supervisor:

- **`weather-ui-agent`** — produced the UI/layout & interaction spec (glass panel, map-click flow,
  search flow, legend, responsive bottom sheet).
- **`weather-data-agent`** — produced the exact Open-Meteo request URLs and the JSON fields to read
  for current/hourly/daily data.
- **`weather-qa-agent`** — reviewed the implemented app for correctness, accessibility, and UX; its
  findings drove a revision round.

See the repository's `docs/multi-agent-weather-demo.md` for the run IDs, the agents' outputs, the QA
findings, and the revisions applied.

## Files

| File | What it is |
| --- | --- |
| `index.html` | Markup, states, and the Leaflet include (pinned + SRI). |
| `styles.css` | Layout, theming (light/dark), responsive sidebar ↔ bottom sheet. |
| `app.js` | Map, search, geolocation, Open-Meteo/Nominatim fetching, rendering, unit toggle. |
