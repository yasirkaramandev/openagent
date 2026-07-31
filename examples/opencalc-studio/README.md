# OpenCalc Studio

OpenCalc Studio is a focused, accessible calculator for the browser. It combines
a familiar standard keypad with scientific functions, local calculation
history, calculator memory, configurable display preferences, and an
installable offline PWA. Calculations run entirely in the browser; the app has
no backend, accounts, analytics, or telemetry.

## Features

- **Standard calculator:** addition, subtraction, multiplication, division,
  powers, percentages, sign changes, clear entry, clear all, and backspace.
- **Scientific mode:** sine, cosine, tangent, inverse trigonometric functions,
  natural and base-10 logarithms, square root, square, arbitrary powers,
  reciprocal, factorial, π, and Euler's number.
- **DEG and RAD:** choose degrees or radians for trigonometric inputs and
  inverse-trigonometric results.
- **History:** keep up to 50 completed calculations locally, reuse an
  expression, remove individual entries, or clear the list.
- **Memory:** session-scoped `MC`, `MR`, `M+`, `M−`, and `MS` controls. Memory
  is not written to browser storage.
- **Display settings:** enable or disable thousands grouping and choose from
  0–12 decimal places. Trailing fractional zeroes are omitted.
- **Private mode:** stop saving new history and remove the currently stored
  history.
- **Themes:** light, dark, or system-controlled color theme, with reduced-motion
  support.
- **Keyboard and paste input:** use the number row or numeric keypad, common
  operator keys, and safely paste arithmetic expressions.
- **Offline PWA:** install the production app from supported browser UI and use
  the precached calculator after the first successful load.

History and settings use `localStorage`. If storage is unavailable, the
calculator still works and keeps usable state for the current page session.

## Quick start

From `examples/opencalc-studio`:

```bash
npm install
npm run dev
```

Vite prints the local development URL. To create and inspect a production
build:

```bash
npm run build
npm run preview
```

The service worker is registered only in production, so use the preview build
when checking installation, offline behavior, or the update prompt.

### Install for offline use

Build and serve the production app, open it once while online, then use the
browser's install or add-to-home-screen command when available. Wait for the
service worker to finish installing before testing an offline reload. A
deployed copy must be served over HTTPS; localhost is accepted as a secure
context for local preview.

## Scripts

| Command                   | Purpose                                                                                |
| ------------------------- | -------------------------------------------------------------------------------------- |
| `npm run dev`             | Start the Vite development server.                                                     |
| `npm run build`           | Type-check the project build and create the production bundle in `dist/`.              |
| `npm run preview`         | Serve the existing production bundle locally with Vite Preview.                        |
| `npm test`                | Run all engine and React component tests once with Vitest.                             |
| `npm run test:components` | Run the React UI test files only.                                                      |
| `npm run test:watch`      | Run Vitest in watch mode.                                                              |
| `npm run test:e2e`        | Build and preview the app, then run Playwright tests in Chromium, Firefox, and WebKit. |
| `npm run typecheck`       | Run strict TypeScript checking without emitting files.                                 |
| `npm run generate:icons`  | Regenerate the 192 px, 512 px, and maskable PWA PNG icons.                             |

Playwright browser binaries must be installed before the first end-to-end run
if they are not already available:

```bash
npx playwright install
```

## Keyboard shortcuts

Calculator shortcuts apply when focus is not inside a button, link, or form
control. Focused controls retain their normal browser keyboard behavior.

| Key              | Action                                                               |
| ---------------- | -------------------------------------------------------------------- |
| `0`–`9`          | Enter a digit.                                                       |
| `.` or `,`       | Enter a decimal point.                                               |
| `+`              | Add.                                                                 |
| `-`              | Subtract.                                                            |
| `*`, `x`, or `X` | Multiply.                                                            |
| `/`              | Divide.                                                              |
| `^`              | Raise to a power.                                                    |
| `%`              | Apply percent to the current entry.                                  |
| `Enter` or `=`   | Evaluate the expression.                                             |
| `Escape`         | Clear all; if History or Settings is open, close that panel instead. |
| `Delete`         | Clear the current entry.                                             |
| `Backspace`      | Delete the last digit or step back to the preceding entry.           |

`Tab` and `Shift+Tab` move through controls, while `Enter` or `Space` activates
the focused control using standard browser behavior. There are no direct
shortcuts for scientific functions, memory controls, sign change, modes, or
panels; reach those controls with the keyboard focus order.

For paste behavior, focus rules, and the exact accepted expression syntax, see
[docs/shortcuts.md](docs/shortcuts.md).

## Browser support

The application targets ES2020-capable, current browser engines. The automated
end-to-end suite runs against Playwright's desktop Chromium, Firefox, and
WebKit projects; it also covers keyboard operation, a mobile-sized viewport,
and an axe accessibility scan.

| Engine tested | Coverage                                                                        |
| ------------- | ------------------------------------------------------------------------------- |
| Chromium      | Calculation, persistence, keyboard input, responsive layout, and accessibility. |
| Firefox       | The same Playwright end-to-end scenarios as Chromium.                           |
| WebKit        | The same scenarios using Playwright's desktop Safari profile.                   |

Core calculation works without PWA APIs. Installation and offline launch
require a browser with Web App Manifest and service-worker support and a secure
context (HTTPS, or localhost for development). The exact install command and
standalone-app experience depend on the browser and operating system.

## How it is built

The app uses React 18, strict TypeScript, Vite, and `vite-plugin-pwa`. Its
framework-independent calculation engine follows a
`tokenizer → parser → evaluator` pipeline and uses integer-scaled decimal
operations for core arithmetic. See [docs/architecture.md](docs/architecture.md)
and [docs/precision.md](docs/precision.md) for the implementation details.

All product code in OpenCalc Studio was authored by OpenAgent agent runs. The
run IDs, commits, changed files, and verification records are documented in
[docs/openagent-build-manifest.json](docs/openagent-build-manifest.json).
