# Keyboard shortcuts

OpenCalc Studio supports direct keyboard entry for the standard calculation
actions. The bindings are defined in `src/ui/Calculator.tsx`; the accessible
shortcut metadata shown on keypad buttons is defined in `src/ui/Keypad.tsx`.

## Calculator bindings

| Key         | Action                | Notes                                                                                                                           |
| ----------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `0`–`9`     | Enter a digit         | Each direct keypad entry is limited to 16 digits.                                                                               |
| `.`         | Enter a decimal point | A second decimal point in the current entry is ignored.                                                                         |
| `,`         | Enter a decimal point | Keyboard alias for `.`; the display still uses a dot.                                                                           |
| `+`         | Add                   |                                                                                                                                 |
| `-`         | Subtract              |                                                                                                                                 |
| `*`         | Multiply              |                                                                                                                                 |
| `x` or `X`  | Multiply              | Case-insensitive alias for `*`.                                                                                                 |
| `/`         | Divide                |                                                                                                                                 |
| `^`         | Raise to a power      | Opens the next operand in the same way as the scientific `xʸ` button.                                                           |
| `%`         | Percent               | For `+` and `-`, percentage is based on the expression to the left; otherwise the current entry is divided by 100.              |
| `Enter`     | Equals                | Evaluates the current or recalled expression.                                                                                   |
| `=`         | Equals                | Alias for `Enter`.                                                                                                              |
| `Escape`    | Clear all             | If History or Settings is open, closes that panel instead and leaves the calculation intact.                                    |
| `Delete`    | Clear entry           | Resets only the current entry to zero.                                                                                          |
| `Backspace` | Backspace             | Deletes the last digit; after an operator, steps back to the preceding entry; after a completed result, clears the calculation. |

The global handler briefly highlights the matching on-screen standard key.

## Focus and modifier behavior

Calculator bindings are intentionally skipped when the key event starts inside
a `button`, `input`, `select`, `textarea`, or link. This prevents calculator
input from interfering with settings fields and focused controls.

Use normal browser navigation for the rest of the interface:

| Key                | Browser behavior in OpenCalc              |
| ------------------ | ----------------------------------------- |
| `Tab`              | Move to the next interactive control.     |
| `Shift+Tab`        | Move to the previous interactive control. |
| `Enter` or `Space` | Activate the currently focused button.    |

`Control`, `Command`, and `Alt` key combinations are ignored by the calculator
`keydown` handler. Shifted symbols such as `+`, `*`, `^`, and `%` still work
because `Shift` alone is not rejected.

There are no dedicated global bindings for:

- positive/negative sign toggle;
- standard/scientific or DEG/RAD mode changes;
- scientific functions and constants;
- `MC`, `MR`, `M+`, `M−`, or `MS`;
- opening History or Settings; or
- selecting the light, system, or dark theme.

All of those actions remain keyboard-accessible through the native focus order.

## Pasting an expression

Use the platform paste command, normally `Ctrl+V` or `Command+V`, while the
calculator page—not an editable form control—is the target. Paste is not
intercepted while History or Settings is open.

Accepted clipboard text:

- is 1–256 characters and no more than 256 tokenizer tokens;
- contains ASCII digits, `.`, whitespace, `+`, `-`, `*`, `/`, `%`, `^`, `(`,
  `)`, or `!`; and
- must tokenize entirely as numbers, operators, or parentheses.

The accepted tokens are reconstructed into a canonical expression and
evaluated immediately. Examples include:

```text
(12 + 3) * 2
5!
2 ^ (3 + 1)
```

Paste rejects commas, `x`, `=`, function names, constants such as `pi`, HTML,
and other text. Rejected content is never passed raw to the evaluator; the
display shows “Paste numbers and calculator operators only.”
