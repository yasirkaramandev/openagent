import { tokenize } from '../calculator';

const MAX_PASTED_EXPRESSION_LENGTH = 256;
const MAX_PASTED_TOKENS = 256;
const ALLOWED_PASTE_CHARACTERS = /^[0-9+\-*/^().!\t\n\r ]+$/;

/**
 * Convert clipboard text into a canonical calculator expression.
 *
 * Clipboard input is deliberately narrower than the full scientific grammar:
 * only ASCII numbers, unambiguous arithmetic operators, parentheses, and
 * whitespace are accepted. `%` is deliberately excluded because the engine
 * uses it for binary remainder while the calculator key uses standard unary
 * percent semantics. The existing tokenizer is the final authority, and
 * callers pass only its reconstructed token stream to the calculation engine.
 */
export function sanitizePastedExpression(value: string): string | null {
  if (
    value.length === 0 ||
    value.length > MAX_PASTED_EXPRESSION_LENGTH ||
    !ALLOWED_PASTE_CHARACTERS.test(value)
  ) {
    return null;
  }

  try {
    const tokens = tokenize(value).filter(({ kind }) => kind !== 'eof');
    if (tokens.length === 0 || tokens.length > MAX_PASTED_TOKENS) return null;
    if (
      tokens.some(
        ({ kind }) =>
          kind !== 'number' &&
          kind !== 'operator' &&
          kind !== 'lparen' &&
          kind !== 'rparen',
      )
    ) {
      return null;
    }
    return tokens.map(({ value: tokenValue }) => tokenValue).join(' ');
  } catch {
    return null;
  }
}
