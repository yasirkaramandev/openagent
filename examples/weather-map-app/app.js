/* Weather Map — application logic.
 *
 * Data sources (all keyless):
 *   - Forecast:  Open-Meteo   https://api.open-meteo.com/v1/forecast   (weather-data-agent spec)
 *   - Geocoding: Open-Meteo   https://geocoding-api.open-meteo.com/v1/search
 *   - Reverse geocoding: Nominatim (OpenStreetMap) for map-click place names
 *   - Tiles:     OpenStreetMap
 *
 * Privacy: the user's location is used only to centre the map + fetch weather. It is never stored
 * (no localStorage / cookies) and never sent anywhere except the keyless weather/geocoding APIs.
 */
"use strict";

(function () {
  // ---- WMO weather-code → { icon, label } (Open-Meteo uses WMO codes) --------------------------
  const WMO = {
    0: ["☀️", "Clear sky"], 1: ["🌤️", "Mainly clear"], 2: ["⛅", "Partly cloudy"],
    3: ["☁️", "Overcast"], 45: ["🌫️", "Fog"], 48: ["🌫️", "Rime fog"],
    51: ["🌦️", "Light drizzle"], 53: ["🌦️", "Drizzle"], 55: ["🌧️", "Dense drizzle"],
    56: ["🌧️", "Freezing drizzle"], 57: ["🌧️", "Freezing drizzle"],
    61: ["🌦️", "Light rain"], 63: ["🌧️", "Rain"], 65: ["🌧️", "Heavy rain"],
    66: ["🌧️", "Freezing rain"], 67: ["🌧️", "Freezing rain"],
    71: ["🌨️", "Light snow"], 73: ["🌨️", "Snow"], 75: ["❄️", "Heavy snow"], 77: ["🌨️", "Snow grains"],
    80: ["🌦️", "Rain showers"], 81: ["🌧️", "Rain showers"], 82: ["⛈️", "Violent showers"],
    85: ["🌨️", "Snow showers"], 86: ["❄️", "Snow showers"],
    95: ["⛈️", "Thunderstorm"], 96: ["⛈️", "Thunderstorm + hail"], 99: ["⛈️", "Thunderstorm + hail"],
  };
  const wmo = (code) => WMO[code] || ["❓", "Unknown"];

  // ---- state -----------------------------------------------------------------------------------
  let unit = "c";                 // "c" | "f"
  let last = null;                // { lat, lon, name, data } — for unit re-render + retry
  let marker = null;
  let fetchController = null;      // AbortController for the in-flight weather request
  let suggestController = null;    // AbortController for the in-flight suggestion request
  let activeSuggestion = -1;
  let suggestions = [];

  // ---- DOM -------------------------------------------------------------------------------------
  const $ = (id) => document.getElementById(id);
  const panel = $("panel");
  const states = { empty: $("state-empty"), loading: $("state-loading"), error: $("state-error"), data: $("state-data") };

  function showState(name) {
    for (const [k, el] of Object.entries(states)) el.hidden = k !== name;
    if (window.matchMedia("(max-width: 640px)").matches && name !== "empty") {
      panel.classList.remove("collapsed");
    }
  }

  // ---- unit helpers ----------------------------------------------------------------------------
  const toF = (c) => (c * 9) / 5 + 32;
  function temp(c) {
    if (c === null || c === undefined || Number.isNaN(c)) return "—";
    const v = unit === "f" ? toF(c) : c;
    return `${Math.round(v)}°`;
  }
  const windDir = (deg) => {
    if (deg === null || deg === undefined) return "";
    const dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
    return dirs[Math.round(deg / 45) % 8];
  };

  // ---- map -------------------------------------------------------------------------------------
  const map = L.map("map", { zoomControl: true, worldCopyJump: true }).setView([25, 10], 2.4);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);

  map.on("click", (e) => select(e.latlng.lat, e.latlng.lng, null));

  function placeMarker(lat, lon) {
    if (marker) marker.setLatLng([lat, lon]);
    else marker = L.marker([lat, lon]).addTo(map);
  }

  // ---- networking with graceful failure --------------------------------------------------------
  async function getJSON(url, signal) {
    const res = await fetch(url, { signal, headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error(`Service responded ${res.status}`);
    return res.json();
  }

  // ---- the main flow: pick a location, load + render weather ------------------------------------
  async function select(lat, lon, name) {
    if (fetchController) fetchController.abort();
    fetchController = new AbortController();
    const signal = fetchController.signal;

    // Record the target BEFORE the request (weather-qa-agent finding): otherwise Retry does nothing
    // on the first-ever failure and retries a *previous* location after a later one fails.
    last = { lat, lon, name };
    hideSuggestions();
    placeMarker(lat, lon);
    map.panTo([lat, lon]);
    showState("loading");
    $("loading-label").textContent = name ? `Loading weather for ${name}…` : "Loading weather…";

    try {
      // If we have no name (map click), reverse-geocode via Nominatim (best-effort; non-fatal).
      let placeName = name;
      if (!placeName) {
        placeName = await reverseGeocode(lat, lon, signal).catch(() => null);
      }

      const url =
        "https://api.open-meteo.com/v1/forecast" +
        `?latitude=${lat.toFixed(4)}&longitude=${lon.toFixed(4)}` +
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,precipitation," +
        "weather_code,wind_speed_10m,wind_direction_10m" +
        "&hourly=temperature_2m,precipitation_probability,weather_code" +
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max" +
        "&timezone=auto&forecast_days=7";

      const data = await getJSON(url, signal);
      last = { lat, lon, name: placeName, data };
      render(last);
    } catch (err) {
      if (err.name === "AbortError") return; // superseded by a newer selection
      showError(err.message || "Could not load weather.");
    }
  }

  async function reverseGeocode(lat, lon, signal) {
    const url = `https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=${lat}&lon=${lon}&zoom=10`;
    const j = await getJSON(url, signal);
    const a = j.address || {};
    return a.city || a.town || a.village || a.county || a.state || j.name || null;
  }

  // ---- rendering -------------------------------------------------------------------------------
  function render({ lat, lon, name, data }) {
    const c = data.current;
    const [icon, label] = wmo(c.weather_code);

    $("place-name").textContent = name || "Selected point";
    $("place-coords").textContent = `${lat.toFixed(3)}, ${lon.toFixed(3)}`;
    $("cur-icon").textContent = icon;
    $("cur-temp").textContent = temp(c.temperature_2m);
    $("cur-cond").textContent = label;
    $("cur-feels").textContent = `Feels like ${temp(c.apparent_temperature)}`;

    const u = data.current_units || {};
    $("m-precip").textContent = `${c.precipitation ?? 0} ${u.precipitation || "mm"}`;
    $("m-pop").textContent = popNow(data);
    $("m-humidity").textContent = `${c.relative_humidity_2m ?? "—"}%`;
    const ws = c.wind_speed_10m;
    $("m-wind").textContent = ws === undefined ? "—" : `${Math.round(ws)} ${u.wind_speed_10m || "km/h"} ${windDir(c.wind_direction_10m)}`;

    renderHourly(data);
    renderDaily(data);
    updateMarkerPopup(name, c, icon);
    showState("data");
  }

  // Open-Meteo returns location-LOCAL timestamps (timezone=auto), e.g. "2026-07-15T12:00" with no
  // offset. Parsing those with `new Date(t)` uses the *viewer's* timezone, so "now" and the shown
  // hour drift whenever the place's timezone differs from the browser's. Compare in the location's
  // own clock instead: shift real "now" by the location's offset and parse each stamp as if UTC.
  const nowInLocationClock = (data) => Date.now() + (data.utc_offset_seconds || 0) * 1000;
  const stampMs = (t) => Date.parse(t + "Z");           // location-local time read on a UTC clock
  const locationHour = (t) => new Date(t + "Z").getUTCHours();

  // Current precipitation probability = the hourly value at/after "now".
  function popNow(data) {
    const h = data.hourly;
    if (!h || !h.time) return "—";
    const now = nowInLocationClock(data);
    for (let i = 0; i < h.time.length; i++) {
      if (stampMs(h.time[i]) >= now) return `${h.precipitation_probability[i] ?? 0}%`;
    }
    return `${h.precipitation_probability?.[0] ?? 0}%`;
  }

  function renderHourly(data) {
    const h = data.hourly;
    const box = $("hourly");
    box.innerHTML = "";
    if (!h || !h.time) return;
    const now = nowInLocationClock(data);
    let start = h.time.findIndex((t) => stampMs(t) >= now);
    if (start < 0) start = 0;
    for (let i = start; i < Math.min(start + 24, h.time.length); i++) {
      const [ic] = wmo(h.weather_code[i]);
      const el = document.createElement("div");
      el.className = "hour";
      el.innerHTML =
        `<div class="h-time">${locationHour(h.time[i]).toString().padStart(2, "0")}:00</div>` +
        `<div class="h-icon">${ic}</div>` +
        `<div class="h-temp">${temp(h.temperature_2m[i])}</div>` +
        `<div class="h-pop">${h.precipitation_probability[i] ?? 0}%</div>`;
      box.appendChild(el);
    }
  }

  function renderDaily(data) {
    const d = data.daily;
    const list = $("daily");
    list.innerHTML = "";
    if (!d || !d.time) return;
    // Parse the date-only stamps as UTC so a negative-offset viewer doesn't see the weekday shift
    // back by a day.
    const fmt = new Intl.DateTimeFormat(undefined, { weekday: "short", timeZone: "UTC" });
    for (let i = 0; i < d.time.length; i++) {
      const day = i === 0 ? "Today" : fmt.format(new Date(d.time[i] + "T00:00:00Z"));
      const [ic] = wmo(d.weather_code[i]);
      const li = document.createElement("li");
      li.innerHTML =
        `<span class="d-day">${day}</span>` +
        `<span class="d-icon" title="${wmo(d.weather_code[i])[1]}">${ic}</span>` +
        `<span class="d-range"><span class="d-min">${temp(d.temperature_2m_min[i])}</span>` +
        `<span class="d-bar"></span>${temp(d.temperature_2m_max[i])}</span>`;
      list.appendChild(li);
    }
  }

  function updateMarkerPopup(name, c, icon) {
    if (!marker) return;
    marker
      .bindPopup(`<strong>${icon} ${temp(c.temperature_2m)}</strong><br>${name || "Selected point"}`)
      .openPopup();
  }

  // ---- search (Open-Meteo geocoding, debounced suggestions) ------------------------------------
  const searchInput = $("search-input");
  const suggBox = $("suggestions");
  let debounce;

  searchInput.addEventListener("input", () => {
    clearTimeout(debounce);
    const q = searchInput.value.trim();
    if (q.length < 2) return hideSuggestions();
    debounce = setTimeout(() => loadSuggestions(q), 250);
  });

  async function loadSuggestions(q) {
    if (suggestController) suggestController.abort();
    suggestController = new AbortController();
    try {
      const url = `https://geocoding-api.open-meteo.com/v1/search?name=${encodeURIComponent(q)}&count=6&language=en&format=json`;
      const j = await getJSON(url, suggestController.signal);
      suggestions = j.results || [];
      renderSuggestions();
    } catch (err) {
      if (err.name !== "AbortError") hideSuggestions();
    }
  }

  function renderSuggestions() {
    suggBox.innerHTML = "";
    activeSuggestion = -1;
    if (!suggestions.length) return hideSuggestions();
    suggestions.forEach((r, i) => {
      const li = document.createElement("li");
      li.setAttribute("role", "option");
      li.id = `sugg-${i}`;
      const where = [r.admin1, r.country].filter(Boolean).join(", ");
      li.innerHTML = `${r.name}${where ? ` <span class="sub">${where}</span>` : ""}`;
      li.addEventListener("click", () => choose(r));
      suggBox.appendChild(li);
    });
    suggBox.hidden = false;
  }

  function hideSuggestions() { suggBox.hidden = true; suggBox.innerHTML = ""; suggestions = []; activeSuggestion = -1; }

  function choose(r) {
    searchInput.value = r.name;
    hideSuggestions();
    map.setView([r.latitude, r.longitude], 9);
    select(r.latitude, r.longitude, r.name);
  }

  // keyboard navigation of the suggestion list
  searchInput.addEventListener("keydown", (e) => {
    if (suggBox.hidden || !suggestions.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      activeSuggestion += e.key === "ArrowDown" ? 1 : -1;
      activeSuggestion = (activeSuggestion + suggestions.length) % suggestions.length;
      [...suggBox.children].forEach((li, i) =>
        li.setAttribute("aria-selected", i === activeSuggestion ? "true" : "false"));
    } else if (e.key === "Enter" && activeSuggestion >= 0) {
      e.preventDefault();
      choose(suggestions[activeSuggestion]);
    } else if (e.key === "Escape") {
      hideSuggestions();
    }
  });

  $("search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    if (activeSuggestion >= 0) return choose(suggestions[activeSuggestion]);
    const q = searchInput.value.trim();
    if (q.length >= 2) loadSuggestions(q).then(() => { if (suggestions[0]) choose(suggestions[0]); });
  });

  // ---- geolocation -----------------------------------------------------------------------------
  $("locate-btn").addEventListener("click", () => {
    if (!navigator.geolocation) return showError("Geolocation isn't available in this browser.");
    showState("loading");
    $("loading-label").textContent = "Finding your location…";
    navigator.geolocation.getCurrentPosition(
      (pos) => { map.setView([pos.coords.latitude, pos.coords.longitude], 10); select(pos.coords.latitude, pos.coords.longitude, "My location"); },
      (err) => showError(err.code === 1 ? "Location permission denied." : "Couldn't get your location."),
      { enableHighAccuracy: false, timeout: 10000, maximumAge: 60000 }
    );
  });

  // ---- unit toggle, retry, mobile sheet --------------------------------------------------------
  document.querySelectorAll(".unit").forEach((btn) => {
    btn.addEventListener("click", () => {
      unit = btn.dataset.unit;
      document.querySelectorAll(".unit").forEach((b) => {
        const on = b === btn;
        b.classList.toggle("active", on);
        b.setAttribute("aria-pressed", String(on));
      });
      if (last && last.data) render(last);  // only re-render once weather has actually loaded
    });
  });

  $("retry-btn").addEventListener("click", () => { if (last) select(last.lat, last.lon, last.name); });
  $("panel-toggle").addEventListener("click", () => panel.classList.toggle("collapsed"));

  function showError(msg) { $("error-message").textContent = msg; showState("error"); }

  // start on the empty state
  showState("empty");
})();
