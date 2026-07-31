# OpenCalc Studio final integration review

Reviewed: 2026-07-25

Scope: `examples/opencalc-studio/`

## Verdict

VERDICT: SHIP

All blocker and major findings discovered in this review were fixed with
targeted changes. The requested unit, static-analysis, production-build, and
cross-browser suites pass at their expected counts. The remaining findings are
low-severity limitations or cleanup opportunities: history replay uses the
current calculation context, CSP clickjacking protection requires a deployment
header, the update action has no visible failure state, and a few internal
exports/helpers are unused.

## What was checked

### Correctness and UI wiring

- Traced the complete `tokenize -> parse -> evaluate -> format` path and the
  calculator's entry/result token flow.
- Checked operator precedence, right-associative power, unary minus, postfix
  factorial, implicit multiplication, scientific notation, chained decimal
  arithmetic, overflow/domain/division errors, and DEG/RAD conversions.
- Exercised standard calculator percent behavior (`200 + 10% = 220`),
  repeat-equals (`7 * 8 = = 448`), UI sign toggle, scientific-mode angle
  changes, and the accessible division-by-zero message.
- Confirmed every `CalcErrorCode` now has a compile-time-exhaustive
  user-facing message and unexpected errors fail closed with a generic message.

### State and persistence

- Inspected the versioned `opencalc.settings` and `opencalc.history` payloads,
  type/range validation, maximum 50-entry cap, unknown-version behavior, and
  guarded storage reads/writes.
- Exercised malformed JSON history recovery, unsupported settings-version
  recovery, settings restoration after remount, and unavailable/quota-style
  storage exception paths by code inspection.
- Confirmed enabling private mode clears loaded history, removes the persisted
  history key, and makes `addEntry` a no-op until private mode is disabled.
- Confirmed memory is intentionally session-only. Its `M` indicator is driven
  by `memory !== null`, so stored zero correctly counts as a value, while MC/MR
  are disabled only when the register is empty.

### Accessibility

- Checked accessible names, pressed states, result/error live announcement,
  logical source order, visible focus styling, Escape behavior, and reduced
  motion.
- Verified opening either side panel focuses its close control; Tab/Shift+Tab
  are contained within the modal; closing by button, backdrop, Escape, or
  history reuse restores focus to the opener.
- Audited CSS target sizes. Calculator, toolbar, memory, panel, theme, and PWA
  update controls are now at least 44 by 44 CSS pixels.
- Ran axe-core on the main calculator route in Chromium, Firefox, and WebKit;
  no serious or critical violations were reported.

### PWA and offline behavior

- Inspected the generated service worker and manifest after a production build.
  The worker uses revisioned precache entries, the
  `opencalc-studio-v1` prefix, `cleanupOutdatedCaches()`, a waiting-worker
  `SKIP_WAITING` message, and an `index.html` navigation fallback.
- Confirmed update registration is production-only and prompt-based. A waiting
  worker does not reload the page until the user selects **Reload**; dismissing
  clears the pending prompt.
- Ran a production Chromium check: the active cache was
  `opencalc-studio-v1-precache-v2-http://127.0.0.1:4173/`, the page was
  service-worker controlled, an offline reload succeeded, `7 + 8` produced
  `15`, and no console/page errors occurred.

### Security and consistency

- Reviewed the CSP directive by directive and searched application-owned
  source/configuration for `eval`, `new Function`,
  `dangerouslySetInnerHTML`, user-derived dynamic imports, remote scripts,
  analytics, and browser network APIs. None are used. The only dynamic import
  is the fixed build-time `virtual:pwa-register` module.
- Checked paste length/token limits, ASCII allowlisting, tokenizer-based
  reconstruction, malformed-number handling, deep input bounds, and rejection
  of identifiers/markup. Ambiguous `%` paste is now rejected because `%` means
  binary remainder in the engine but unary percent on the calculator keypad.
- Compared exports/imports and noted duplicated storage helpers, redundant
  aliases, and unused internal exports/parameters.

## Findings by severity

### Blocker

None.

### Major

1. **Fixed — scientific-notation results were unsafe to reuse.** `format()`
   can produce values such as `1e-5`, but the tokenizer and decimal parser did
   not support exponent literals. Feeding such a result into a following
   operation could be rejected or interpreted as multiplication by constant
   `e`. The tokenizer and decimal parser now support bounded exponent syntax,
   while still rejecting malformed forms such as `1..2`.

2. **Fixed — chained decimal operations reintroduced binary noise.**
   Intermediate numbers were converted with 21 significant digits, turning an
   exact `0.3` intermediate back into
   `0.299999999999999988...`. Promotion now uses JavaScript's canonical
   shortest round-trippable string. The suite covers
   `0.1 + 0.2 - 0.3 === 0`.

3. **Fixed — repeat-equals did nothing.** Pressing equals immediately after a
   completed binary calculation returned early. The calculator now retains the
   final operator and operand and repeats them on subsequent equals presses.

4. **Fixed — DEG/RAD changes left a stale result on screen.** Changing angle
   mode now re-evaluates the last completed expression under the selected mode
   without silently adding another history entry. Cross-browser coverage checks
   `sin(30)` changing from `0.5` in DEG to `-0.9880316241` in RAD.

5. **Fixed — modal focus escaped and was not restored.** The history/settings
   panels initially focused their close buttons, but background controls
   remained reachable by Tab and closing left focus on the document body.
   Keyboard focus is now contained while a panel is open and restored to the
   correct opener when it closes.

6. **Fixed — paste exposed conflicting percent semantics.** The keypad uses
   conventional unary/calculator percent semantics, while the pure engine uses
   `%` for binary remainder. Paste previously routed `%` directly to the engine,
   so identical visible input had different meanings by input channel. Paste
   now rejects `%`; public engine remainder behavior is unchanged.

### Minor

1. **History does not store an evaluation-context snapshot.** Schema version 1
   stores expression, formatted engine result, timestamp, and ID, but not angle
   mode or precision. Reusing an entry intentionally re-evaluates it under the
   current settings, so a scientific expression can produce a different result
   than the value shown in the history row. This behavior is documented in
   `architecture.md`, but the row itself does not expose the original context.

2. **`frame-ancestors` cannot be enforced by the current meta CSP.** The
   in-document policy is otherwise restrictive and complete for the app's
   resources (`default-src 'none'`, self-only scripts/styles/assets/workers,
   and no objects, frames, bases, or forms). A production host should emit the
   same CSP as an HTTP response header and add `frame-ancestors 'none'` for
   clickjacking defense.

3. **The PWA update action has no failure UI.** The Reload button invokes the
   update promise without disabling repeated clicks or surfacing a rejected
   update. Normal Workbox update behavior is correct, but an unusual service
   worker failure would leave the prompt present without an explanation.

### Nit

1. `decimal.ts` exports an unused `intPow` and re-exports `CalcError`;
   `scientific.ts` exposes unused `factorialScientific`/`factorial` aliases.
   `index.ts` also contains redundant same-name `as` aliases. These do not enter
   the production bundle when unused.

2. `roundToSignificant` accepts an intentionally unused `sig` argument, and
   `powOp` accepts two node arguments only to discard them. The signatures make
   the implementation look more extensible than it currently is.

3. Settings and history duplicate their small `storageAvailable()` helper.
   Keeping them separate is harmless but slightly increases maintenance
   surface.

4. The generated precache manifest lists the manifest and icons through both
   plugin-managed entries and the configured glob. The duplicate URL/revision
   pairs are harmless but make the reported 13-entry list noisier than the
   unique asset set.

## Fixes made

- Added exponent-literal support and malformed-number rejection to the
  tokenizer/decimal boundary.
- Preserved exact finite decimal intermediates when returning them to the
  integer-scaled core.
- Added repeat-equals state and live DEG/RAD re-evaluation.
- Made the typed error-message table exhaustive and corrected the factorial
  wording to include zero.
- Removed ambiguous percent from paste input and made the rejection message
  explicit.
- Added modal focus containment/restoration and brought PWA prompt controls up
  to the 44-pixel target.
- Extended existing test cases without weakening/deleting tests or changing the
  expected totals.

## Verification

Run from `examples/opencalc-studio/` after the final changes:

| Command                | Exact result                                                                  |
| ---------------------- | ----------------------------------------------------------------------------- |
| `npm run format:check` | PASS — all matched files use Prettier formatting.                             |
| `npm run lint`         | PASS — ESLint completed with 0 errors and 0 warnings.                         |
| `npm run typecheck`    | PASS — `tsc --noEmit` completed with no diagnostics.                          |
| `npm test`             | PASS — 49/49 tests (44 engine + 5 UI), 2/2 test files.                        |
| `npm run build`        | PASS — 51 modules transformed; PWA generated a 13-entry, 208.19 KiB precache. |
| `npx playwright test`  | PASS — 18/18 tests across Chromium, Firefox, and WebKit.                      |

No tests were deleted, skipped, weakened, or marked flaky to obtain these
results.
