# OpenCalc Studio architecture

OpenCalc Studio is a client-only React and TypeScript application built with
Vite and shipped as a PWA. The current implementation has four main concerns:
a framework-independent calculation engine, a React UI, small persistence hooks
backed by `localStorage`, and a generated Workbox service worker.

This document supersedes stale paths and planned components from the original
architecture draft. The durable principles from that draft still apply:

- **Engine/UI separation.** `src/calculator/` imports neither React nor browser
  APIs.
- **Deterministic core.** An expression and the same `EvalContext` produce the
  same engine result without consulting locale, storage, session, or clock.
- **Typed, fail-closed errors.** Invalid expressions and invalid mathematical
  operations throw a `CalcError` subclass instead of returning a fallback.
- **No engine singleton.** Angle mode, formatting precision, and maximum
  evaluation depth are passed in an `EvalContext`.
- **Formatting at the boundary.** Grouping and decimal-separator choices do not
  alter the expression AST or arithmetic operations.

## Runtime map

```text
src/main.tsx
└── App
    ├── useCalculatorSettings ─────────── localStorage: opencalc.settings
    ├── ThemeControl / useTheme
    ├── Calculator
    │   ├── Display
    │   ├── Keypad
    │   ├── ScientificKeypad
    │   ├── HistoryPanel ──────────────── localStorage: opencalc.history
    │   └── SettingsPanel
    └── PwaUpdatePrompt

Calculator action or accepted paste
└── compute(expression, context)
    ├── tokenize(expression)
    ├── parse(tokens)
    ├── evaluate(AST, context)
    └── format(value, { precision })
```

`src/ui/styles.css` contains the responsive layout, explicit light/dark theme
tokens, system-theme media query, focus styling, and reduced-motion override.

## Engine layer

The engine lives in `src/calculator/` and exposes its public surface through
`src/calculator/index.ts`.

| Module          | Current responsibility                                                                                                                          |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `types.ts`      | `EvalContext`, tokens, AST nodes, formatting options, and the typed `CalcError` hierarchy.                                                      |
| `decimal.ts`    | Internal sign/digits/scale decimal representation, integer-scaled arithmetic, number conversion, and factorial.                                 |
| `tokenizer.ts`  | Converts source text to positioned tokens and rejects unsupported characters or identifiers.                                                    |
| `parser.ts`     | Builds an AST with precedence, right-associative powers, unary signs, postfix factorial, function calls, grouping, and implicit multiplication. |
| `scientific.ts` | Registers scientific functions and constants and applies DEG/RAD conversion and domain checks.                                                  |
| `evaluator.ts`  | Recursively evaluates the AST, enforces maximum node depth, dispatches arithmetic/scientific operations, and formats values.                    |
| `index.ts`      | Re-exports the layers and provides the `compute()` convenience pipeline.                                                                        |

### Pipeline

The normal entry point is:

```ts
compute(expression, {
  angleMode: 'DEG',
  precision: 12,
  maxNodeDepth: 256,
});
```

`compute()` fills omitted context fields from `DEFAULT_CONTEXT`, runs
`tokenize → parse → evaluate → format`, and returns:

```ts
interface ComputeResult {
  value: number;
  formatted: string;
}
```

The lower-level exports remain available for tests and other non-React
consumers:

```ts
tokenize(input: string): Token[]
parse(tokens: Token[]): Node
evaluate(node: Node, ctx?: EvalContext): number
format(value: number, opts?: Partial<FormatOptions>): string
```

The tokenizer accepts ordinary integer and decimal literals, arithmetic
operators (`+ - * / % ^`), parentheses, factorial, commas as function argument
separators, known function names, and the `pi` and `e` constants. The parser
implements implicit multiplication such as `2pi` and `2(3 + 1)`.

### AST and evaluation

The AST is a discriminated union of numeric, binary-operator, unary, function
call, constant, and factorial nodes. Source decimal text is retained privately
on numeric nodes so literal operands can be reconstructed for integer-scaled
decimal operations.

Core `+`, `-`, `*`, and `/` dispatch through `decimal.ts`. Remainder and
exponentiation use JavaScript numeric operations with explicit zero, domain,
and non-finite-result checks. Scientific functions use `Math.*`; trig inputs
are converted to radians in DEG mode, and inverse-trig results are converted
back to the selected mode. See [precision.md](precision.md) for the exact
numeric model and its boundaries.

### Typed errors

Every engine error extends `CalcError`, exposes a stable `code` (also available
as `kind`), and may carry a zero-based source `pos`.

| Code / class       | Typical source                                                                            |
| ------------------ | ----------------------------------------------------------------------------------------- |
| `DivisionByZero`   | Division or modulo by zero, a negative power of zero, or a tangent asymptote.             |
| `DomainError`      | Invalid square root/log/inverse-trig input or a negative base with a fractional exponent. |
| `Overflow`         | A non-finite result, factorial above 170, or the evaluation-depth guard.                  |
| `SyntaxError`      | Unsupported input, malformed grammar, unknown function/constant, or wrong function arity. |
| `InvalidFactorial` | A negative or non-integer factorial operand.                                              |
| `MismatchedParens` | Missing or extra parentheses.                                                             |

`Calculator` catches `CalcError` and maps the code to a short user-facing
message. Unexpected failures use a generic “Unable to calculate” message. The
`Display` result is an `aria-live="polite"` output, so results and errors are
announced without moving focus.

## UI layer

### `App`

`App` is the composition root. It owns the settings hook, applies the theme,
renders the page chrome and header `ThemeControl`, mounts `Calculator`, and
keeps the PWA update prompt available globally.

### `Calculator`

`Calculator` is the stateful UI controller. React state holds the current
expression parts and entry, display/error flags, standard/scientific mode,
open panel, memory value, recalled history expression, and temporary
keyboard-highlight state.

It:

- converts keypad and supported global key events into `CalculatorAction`
  values;
- calls `compute()` with the current angle mode and an engine precision of 12;
- applies the display-only decimal-place and grouping preferences;
- implements calculator percent semantics;
- transforms scientific-key actions into engine expressions;
- implements session-only memory operations;
- appends successful evaluations to history;
- recalls a history expression for re-evaluation; and
- validates pasted expressions through `sanitizePastedExpression()` before
  calling the engine.

Each direct keypad entry is limited to 16 digits. Paste input is separately
limited to 256 characters and 256 tokens and accepts a deliberately narrower
arithmetic-only grammar.

### Presentational components

- `Keypad` defines the standard buttons, accessible names, and advertised
  `aria-keyshortcuts`.
- `ScientificKeypad` exposes sin/cos/tan, inverse trig, ln/log10, square root,
  square, power, reciprocal, factorial, π, and e.
- `Display` renders the current mode, angle mode, memory indicator,
  expression, result, and accessible status/error output.
- `HistoryPanel` is a modal dialog-style side panel for reuse, per-entry
  removal, and clearing.
- `SettingsPanel` edits grouping, decimal places, angle mode, theme, and
  private mode.
- `PwaUpdatePrompt` offers user-controlled reload or dismissal when a waiting
  service worker reports an update.

`CalculatorButton` is the shared accessible button primitive. Native tab order
and button behavior provide access to controls that do not have global
calculator shortcuts.

## State and persistence

No external state library is used. Settings and history are isolated in React
hooks; transient calculator state remains inside `Calculator`.

### Settings schema

`useCalculatorSettings()` reads and writes the `opencalc.settings` key:

```json
{
  "version": 1,
  "settings": {
    "digitGrouping": true,
    "decimalPlaces": 10,
    "angleMode": "DEG",
    "theme": "system",
    "privateMode": false
  }
}
```

These values are also the defaults. `decimalPlaces` is validated as an integer
from 0 through 12; the other fields are validated against their exact boolean
or string unions.

### History schema

`useCalculatorHistory()` reads and writes `opencalc.history`:

```json
{
  "version": 1,
  "entries": [
    {
      "id": "timestamp-sequence",
      "expression": "2 + 3",
      "result": "5",
      "createdAt": 1780000000000
    }
  ]
}
```

History is newest-first and capped at 50 entries. The current schema stores the
expression, the engine-formatted result, and a timestamp; it does **not** store
an angle-mode or precision snapshot. Reusing an entry stages its expression,
and pressing equals evaluates it under the current angle mode.

### Versioning and recovery

Both payloads use schema version `1`. There is currently no migration path:
an unknown version, invalid JSON, wrong field type, out-of-range setting, or
malformed history entry causes that storage key to be removed and safe defaults
to be used.

All storage reads and writes are guarded. If the API exists but throws (for
example, because it is blocked or over quota), settings and history continue
to work in React memory for the current session. Enabling private mode removes
the history key and clears the loaded entry list; new calculations are not
recorded until private mode is disabled.

The memory register, standard/scientific mode, open panel, and in-progress
calculation are never persisted.

## PWA and service-worker strategy

`vite-plugin-pwa` generates the Web App Manifest and a Workbox service worker
during `npm run build`.

- The manifest uses the app name and short name, standalone display mode,
  same-directory start URL and scope, theme/background colors, 192 px and
  512 px icons, and a maskable 512 px icon.
- Workbox precaches generated `html`, `js`, `css`, `png`, `svg`, and
  `webmanifest` files. `index.html` is the navigation fallback.
- `cacheId` is `opencalc-studio-v1`, and outdated generated caches are cleaned
  up. No runtime cache routes, background sync, or push handling are defined.
- The service worker is registered only in a production build and only when
  `navigator.serviceWorker` exists. Registration is immediate.
- Update handling uses `registerType: "prompt"`. A waiting update is announced
  through the small subscription module in `src/pwa.ts`; the UI reloads only
  after the user selects **Reload**, so a calculation is not automatically
  interrupted.
- App code performs no backend requests. Once the generated app shell is
  precached, the engine, UI, icons, and navigation fallback are available
  offline.

Installation and service-worker operation require normal platform support and
a secure context. Development mode intentionally does not register the worker.

## Testing and build boundaries

- Vitest runs the engine in Node and React components in jsdom.
- Playwright builds and serves the production app and runs the same scenarios
  in Chromium, Firefox, and WebKit.
- The production build runs TypeScript project checking before Vite bundling.
- The calculation engine is testable without DOM or React setup.

The original task planning and integration history remain available in
`openagent-task-graph.json` and `openagent-build-manifest.json`. The latter
records that all product code was authored by OpenAgent agent runs.
