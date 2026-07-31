# OpenCalc Studio — Acceptance Criteria

A single acceptance matrix expressed as checklists. Each task in
`openagent-task-graph.json` must satisfy every item in every section that
applies to its deliverables before it is marked complete. The Integration
Review task (`calc-task-008`) re-walks this entire matrix across all phases,
and the Release Hardening task (`calc-task-009`) re-runs the audit-grade
sections (quality, security, accessibility, PWA) against the shipped build.

Legend: `[ ]` pending, `[x]` satisfied. Sections are grouped by acceptance
category. Each item is written so a reviewer (human or agent) can verify it
with a binary yes/no.

---

## 1. Functional Acceptance

### 1.1 Engine pipeline

- [ ] `tokenize(input): Token[]` returns a token stream for every supported
      input form (integers, decimals, scientific notation `1.5e3`, operators,
      parentheses, commas, identifiers, named constants).
- [ ] `parse(tokens): Node` produces a valid AST for the supported grammar,
      respecting operator precedence, associativity, unary minus, implicit
      precedence ambiguity, and function-argument arity.
- [ ] `evaluate(node, ctx): Decimal` returns a canonical `Decimal`; `number` is
      not used for final results.
- [ ] `format(value, opts): string` applies locale/grouping only at the boundary
      and never mutates the canonical internal representation.
- [ ] The pipeline `tokenize -> parse -> evaluate -> format` is the only
      sanctioned execution path; no phase is skipped.

### 1.2 Core arithmetic & errors

- [ ] `a + b`, `a - b`, `a * b`, `a / b` behave per decimal arithmetic.
- [ ] `1 / 0`, `x % 0`, and divide-by-zero-derived cases raise `DivisionByZero`.
- [ ] `sqrt(-1)`, `log(-5)`, `log(0)`, `asin(2)` raise `DomainError`.
- [ ] `10 ** 10000` (and equivalent) raise `Overflow`.
- [ ] Malformed numbers, dangling operators, and unknown identifiers raise
      `SyntaxError`.
- [ ] Unbalanced `(` / `)` raise `MismatchedParens`.

### 1.3 Scientific functions & DEG/RAD

- [ ] `sin`, `cos`, `tan` honor `ctx.angleMode` and produce mode-correct
      results to within configured precision.
- [ ] `asin`, `acos`, `atan` return values in the active angle mode.
- [ ] Hyperbolic functions (`sinh`, `cosh`, `tanh`) are dimensionless and ignore
      angle mode.
- [ ] `log`, `ln`, `exp`, `sqrt`, `pow`, `root`, `abs` exist and respect domain.
- [ ] Factorial `n!` works for non-negative integers; `0.5!`, `(-3)!`, and
      non-integer / negative arguments raise `InvalidFactorial`.
- [ ] Toggling `angleMode` between `DEG` and `RAD` re-evaluates an existing
      expression correctly without caching stale results.
- [ ] Constant `pi` and `e` resolve to canonical decimal values.

### 1.4 History & settings

- [ ] History records expression, canonical result, and the active `ctx`
      snapshot (angle mode, precision).
- [ ] A history entry can be replayed to reproduce the recorded result offline.
- [ ] Settings can change angle mode, precision, theme, and privacy/telemetry
      toggle, and the next evaluation reflects the change.
- [ ] Changing precision does not corrupt previously stored history values.

---

## 2. UI / Responsive Acceptance

- [ ] The app renders correctly at 320 px, 768 px, and 1280 px+ without
      horizontal scrollbars on the calculator viewport.
- [ ] The keypad layout reflows / scales responsively; no clipped buttons at any
      supported breakpoint.
- [ ] The display expression and result remain legible (no overflow truncation
      of the current result) for results up to the configured precision.
- [ ] Theme switching (light / dark / high-contrast) applies without reload and
      respects `prefers-color-scheme` on first load.
- [ ] All interactive controls are reachable and operable by keyboard with a
      visible focus indicator at every breakpoint.
- [ ] No layout shift > 5 % CLS on initial render and on result population.
- [ ] The app is usable and fully offline (see PWA section) with no degraded
      calculator functionality when the network is unavailable.

---

## 3. Quality Acceptance

- [ ] `tsc --noEmit` passes with `strict: true`; no `any`, no `@ts-ignore`,
      no `as unknown as` casts in the engine modules.
- [ ] Vitest unit suite passes with > 90 % statement coverage on
      `src/calculator/**`.
- [ ] An integration test exercises the full pipeline
      `tokenize -> parse -> evaluate -> format` for a representative fixture
      set including each error kind and both angle modes.
- [ ] No console errors or uncaught promise rejections during a standard
      calculation session in a fresh browser profile.
- [ ] Bundle (gzipped) for the engine + UI stays within the agreed budget
      (e.g. PWA initial JS <= 150 KB gzipped); documented in the release notes.
- [ ] Lighthouse performance score >= 90 on a cold start for the calculator
      entry route.
- [ ] Determinism: identical inputs + identical `ctx` produce byte-identical
      `format` output across sessions and across Web Worker vs main thread.

---

## 4. Security Acceptance

- [ ] No use of `eval`, `new Function`, dynamic `import()` of user input, or
      any string-to-code primitive anywhere in the codebase (CI grep gate).
- [ ] Input length and `ctx.maxNodeDepth` are enforced to bound parser depth
      and token-stream size; oversize input raises `SyntaxError` or a typed
      limit error without crashing the renderer.
- [ ] The engine performs no network access and no storage access outside the
      app-controlled history store.
- [ ] No credentials, tokens, or telemetry are read, transmitted, or persisted.
- [ ] The PWA service worker caches only same-origin app assets; no
      cross-origin caching, no open redirect surfaces, no `navigate` fallthrough
      to unexpected scopes.
- [ ] History storage is sandboxed and cannot read or write outside its
      allocated origin/container quota; quota errors surface cleanly (see
      Section 5).
- [ ] A dependency manifest review confirms no `postinstall` scripts from
      untrusted packages and no engines requiring insecure Node versions.
- [ ] Threat-model document lists mitigations for: input limits, decimal-stack
      overflow, history tampering/replay, service-worker scope, and
      offline/sandbox escape.

---

## 5. Accessibility Acceptance

- [ ] The result region is an `aria-live` polite region; error messages are
      announced to assistive technology via the same region.
- [ ] Every `CalcError.code` maps to a user-facing, localized message string.
- [ ] All buttons have accessible names (visible label or `aria-label`); pure
      icon buttons include `aria-label` and `aria-pressed` where stateful.
- [ ] Color contrast meets WCAG 2.1 AA in light, dark, and high-contrast
      themes (>= 4.5:1 for text, >= 3:1 for large text and UI components).
- [ ] The keypad and history list are fully operable by keyboard with logical
      tab order and visible focus rings.
- [ ] Reduced-motion preference (`prefers-reduced-motion`) disables
      non-essential animations.
- [ ] axe-core / Lighthouse a11y audit reports zero serious violations on the
      calculator entry route.
- [ ] Screen-reader walkthrough confirms expression entry, evaluation, history
      replay, and settings changes are all perceivable and operable.

---

## 6. PWA / Offline Acceptance

- [ ] A valid Web App Manifest (name, icons 192 + 512, display standalone,
      theme/background colors, start_url) is installable on Chrome, Edge, and
      Firefox.
- [ ] A service worker is registered and serves the app shell and the engine
      bundle offline with a stale-while-revalidate or cache-first strategy.
- [ ] The app loads and fully functions (calculate, history, settings) with the
      network cable unplugged after the first successful install.
- [ ] Service-worker updates do not break an in-flight session; the UI offers a
      non-intrusive "update available" action rather than auto-reloading.
- [ ] Lighthouse PWA category is fully passing; installability criteria met.
- [ ] No background sync, push, or runtime-only APIs are used unless documented
      as optional and gracefully degraded when unavailable.

---

## 7. Documentation Acceptance

- [ ] A user guide covers keypad usage, keyboard shortcuts, scientific
      functions, and the DEG/RAD toggle.
- [ ] An error-message reference lists every `CalcError.code` with a sample
      input and the surfaced message.
- [ ] An accessibility statement documents the supported AT, contrast modes,
      and keyboard model.
- [ ] A contribution/engine-extension guide explains how to add a new
      scientific function or error kind without breaking the pipeline contract.
- [ ] README and in-app help are consistent with the shipped feature set within
      the release.

---

## 8. Integration & Release Acceptance

- [ ] All sections 1–7 are satisfied, or each open item is explicitly waived
      with rationale recorded in the release notes.
- [ ] The integration review (`calc-task-008`) signs off the cross-phase
      contract: engine exports, `CalcError.code` set, `EvalContext` shape, and
      `FormatOptions` shape are unchanged from what each consumer was built
      against.
- [ ] Release notes enumerate changes, known limitations, and any waived
      acceptance items.
- [ ] The release is reproducible: a clean `npm ci && npm run build && npm test`
      on a supported Node version produces the same outcome twice.
