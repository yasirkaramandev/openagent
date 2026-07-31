# Numeric precision

OpenCalc Studio uses a hybrid decimal model. Core arithmetic is performed with
an internal base-10 representation backed by `bigint`, while the engine's
public `Decimal` type and scientific-function results are JavaScript `number`
values. This avoids familiar errors such as `0.1 + 0.2` for direct decimal
arithmetic, but it is not an arbitrary-precision numeric system.

## Decimal arithmetic

`src/calculator/decimal.ts` decomposes each decimal operand into:

```ts
interface DecimalRep {
  sign: -1 | 0 | 1;
  digits: bigint;
  scale: number;
}
```

The represented value is `sign × digits / 10^scale`.

- Addition and subtraction align the scales and operate on signed `bigint`
  magnitudes.
- Multiplication multiplies the magnitudes and adds the scales.
- Division shifts the dividend to retain 20 fractional digits, performs
  integer division, and normalizes the result. Digits beyond that internal
  division window are truncated.
- Trailing base-10 zeroes are removed from internal representations, and
  negative zero is normalized to zero.
- The parser retains the source text for numeric literal nodes. The evaluator
  uses that source when possible instead of first accepting the literal's
  binary floating-point approximation.

After an arithmetic operation, the internal representation is converted to the
engine's public `number` value. Computed values that participate in later
operations may therefore pass through a binary floating-point conversion.
Remainder (`%`) and exponentiation (`^`) use JavaScript numeric operations
directly.

Scientific functions and constants also use JavaScript `Math`, because
trigonometric, logarithmic, root, π, and e values generally have no finite
base-10 representation. Their results receive the same finite/domain checks
as the rest of the evaluator.

## Calculation precision and display precision

There are two distinct precision controls:

1. `EvalContext.precision` controls significant digits when `compute()` creates
   its engine-formatted string. It does not round every intermediate
   operation. The default is 12.
2. The user's **Decimal places** setting controls the final calculator display.
   `Calculator` still calls the engine with precision 12, then formats a result
   with 0–12 fractional places according to the setting.

The UI uses fixed formatting with `decimalPlaces + 1` passed as the formatter's
significant-precision parameter; in the current formatter this produces
exactly `decimalPlaces` positions through `Number.prototype.toFixed()`.
Trailing fractional zeroes and a trailing decimal point are then removed. The
setting is therefore a maximum number of displayed fractional places, not a
request to pad every result with zeroes.

For example, at 4 decimal places:

- `1 / 3` is displayed as `0.3333`.
- `2 / 4` is displayed as `0.5`, not `0.5000`.
- `2` is displayed as `2`.

Changing the setting affects result presentation only. It does not mutate the
AST, internal arithmetic, memory value, or stored history result.

## Rounding

`format()` supports three notation modes:

- `fixed` uses `toFixed(precision - 1)`.
- `scientific` uses `toExponential(precision - 1)`.
- `auto`, the engine default, uses fixed form when the base-10 order is from
  `-4` through `precision - 1`; otherwise it uses exponential form.

These operations use the JavaScript engine's standard number-rounding
semantics. Non-exponential fixed and auto results have insignificant trailing
zeroes removed. Explicit scientific results retain the digits produced by
`toExponential()`.

With the default precision of 12, auto formatting normally switches to
scientific notation below `1e-4` and at `1e12` or above. Zero is always
rendered as `0`.

The calculator's user-facing result path requests fixed formatting for the
selected decimal-place count. JavaScript itself may still emit exponential
notation for magnitudes at or above `1e21`; engine history values may also be
in exponential form because `compute()` uses auto notation.

Scientific notation is an output format. The expression tokenizer does not
recognize exponent literals: `1.5e3` is not interpreted as 1500. The paste
filter deliberately accepts arithmetic-only input and rejects the `e`
identifier.

## Digit grouping and separators

Digit grouping is applied only while formatting for display:

- when enabled, commas are inserted every three digits in the integer part;
- the setting never changes the numeric value or stored expression;
- exponential strings bypass grouping; and
- the shipped UI always displays `.` as the decimal separator.

The engine formatter can accept another group separator and supports `.` or
`,` as a decimal separator for non-exponential output, but those options are
not exposed in Settings. The keyboard accepts either `.` or `,` as a request
to enter the UI's dot decimal point.

## Overflow and domain handling

The evaluator rejects non-finite and mathematically invalid results rather than
allowing `NaN` or infinity into calculator state.

- Division or modulo by zero is rejected. Zero to a negative power is also a
  divide-by-zero condition.
- A tangent whose cosine is within `1e-15` of zero is treated as an undefined
  asymptote.
- Square root of a negative value, logarithms of non-positive values,
  inverse-sine/cosine outside `[-1, 1]`, and a negative base with a fractional
  exponent are domain errors.
- Factorial accepts non-negative integers through 170. Larger operands
  overflow; negative or fractional operands are invalid factorials.
- Infinite results from powers, conversions, roots, or scientific functions
  overflow.
- Exceeding `EvalContext.maxNodeDepth` also raises `Overflow`; the default
  limit is 256.

The standalone `format()` function contains defensive `NaN`/infinity strings,
but normal `compute()` evaluation fails with typed errors before those values
reach the UI.

## Typed error codes

The actual discriminants in `src/calculator/types.ts` are:

| Code               | Meaning                                                                            | Example     |
| ------------------ | ---------------------------------------------------------------------------------- | ----------- |
| `DivisionByZero`   | A division-like operation has a zero denominator or undefined asymptote.           | `1 / 0`     |
| `DomainError`      | The input is outside the mathematical domain.                                      | `sqrt(-1)`  |
| `Overflow`         | The result is non-finite, the factorial limit is exceeded, or the AST is too deep. | `10 ^ 9999` |
| `SyntaxError`      | The expression contains unsupported text or invalid grammar.                       | `2 +`       |
| `InvalidFactorial` | Factorial received a negative or non-integer operand.                              | `0.5!`      |
| `MismatchedParens` | An opening or closing parenthesis has no match.                                    | `(2 + 3`    |

The divide-by-zero code is named `DivisionByZero`—not `DivideByZero`. Every
concrete error extends `CalcError`, exposes the code as both `error.code` and
`error.kind`, and may include a zero-based source position.

The UI maps those codes to these messages:

| Code               | Display message                         |
| ------------------ | --------------------------------------- |
| `DivisionByZero`   | Cannot divide by zero                   |
| `DomainError`      | That value is outside the valid range   |
| `Overflow`         | Result is too large                     |
| `SyntaxError`      | Check the expression                    |
| `InvalidFactorial` | Factorial needs a positive whole number |
| `MismatchedParens` | Check the parentheses                   |
