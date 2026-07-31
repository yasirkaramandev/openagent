/**
 * tokenizer.ts — convert a raw expression string into a Token stream.
 *
 * Contract (architecture §2.2): `tokenize` is total — it returns a token
 * array or throws `SyntaxError`/`MismatchedParens` for untokenizable input.
 * It produces an `eof` sentinel and never reaches into the parser.
 *
 * Recognised tokens:
 *   - numbers:    digits, optional decimals, and optional exponents,
 *                 e.g. 42, 0.1, .5, 3.14, 1.5e3
 *   - operators:  + - * / % ^
 *   - parens:     ( )
 *   - functions:  sin cos tan asin acos atan sinh cosh tanh
 *                 log10 ln sqrt cbrt
 *   - constants:  pi e
 *   - factorial:  !
 *   - comma:      ,  (function argument separator)
 *
 * Unary minus is NOT folded here — the parser treats leading/in-context `-`
 * as a unary operator. The tokenizer simply emits `-` as an operator token.
 */

import { SyntaxError, type Token, type TokenKind } from './types';

/** Identifier-like names mapped to either a function or a constant. */
const FUNCTION_NAMES = new Set<string>([
  'sin',
  'cos',
  'tan',
  'asin',
  'acos',
  'atan',
  'sinh',
  'cosh',
  'tanh',
  'log10',
  'ln',
  'sqrt',
  'cbrt',
]);

const CONSTANT_NAMES = new Set<string>(['pi', 'e']);

/** Convert raw input into a token stream terminated by an `eof` token. */
export function tokenize(input: string): Token[] {
  const tokens: Token[] = [];
  let i = 0;
  const n = input.length;

  while (i < n) {
    const ch = input[i];
    if (ch === undefined) break;
    const pos = i;

    // Whitespace — skipped, not tokenised.
    if (isWhitespace(ch)) {
      i += 1;
      continue;
    }

    // Numbers (integers, decimals, leading-dot decimals).
    if (isDigit(ch) || (ch === '.' && isDigit(next(input, i)))) {
      const start = i;
      let seenDot = false;
      while (i < n) {
        const c = input[i];
        if (c === undefined) break;
        if (isDigit(c)) {
          i += 1;
        } else if (c === '.' && !seenDot) {
          seenDot = true;
          i += 1;
        } else {
          break;
        }
      }

      // Scientific notation. Treat `e` as the named constant unless it is
      // followed by an optional sign and at least one exponent digit.
      if (input[i] === 'e' || input[i] === 'E') {
        let exponentEnd = i + 1;
        if (input[exponentEnd] === '+' || input[exponentEnd] === '-') {
          exponentEnd += 1;
        }
        if (isDigit(input[exponentEnd] ?? '')) {
          exponentEnd += 1;
          while (isDigit(input[exponentEnd] ?? '')) exponentEnd += 1;
          i = exponentEnd;
        }
      }

      // Do not reinterpret a second decimal point as implicit
      // multiplication (for example, `1..2` must be a malformed number).
      if (input[i] === '.') {
        throw new SyntaxError(`invalid number near pos ${i}`, i);
      }

      const text = input.slice(start, i);
      if (text === '.') {
        throw new SyntaxError(`invalid number near pos ${pos}`, pos);
      }
      tokens.push(mk('number', text, start));
      continue;
    }

    // Identifiers: letters, possibly followed by more letters/digits.
    if (isAlpha(ch)) {
      const start = i;
      while (i < n && (isAlpha(input[i] ?? '') || isDigit(input[i] ?? ''))) {
        i += 1;
      }
      const name = input.slice(start, i);
      if (FUNCTION_NAMES.has(name)) {
        tokens.push(mk('ident', name, start));
      } else if (CONSTANT_NAMES.has(name)) {
        tokens.push(mk('constant', name, start));
      } else {
        throw new SyntaxError(`unknown identifier "${name}"`, start);
      }
      continue;
    }

    // Operators.
    if (
      ch === '+' ||
      ch === '-' ||
      ch === '*' ||
      ch === '/' ||
      ch === '%' ||
      ch === '^'
    ) {
      tokens.push(mk('operator', ch, pos));
      i += 1;
      continue;
    }

    // Parens / comma / factorial.
    if (ch === '(') {
      tokens.push(mk('lparen', ch, pos));
      i += 1;
      continue;
    }
    if (ch === ')') {
      tokens.push(mk('rparen', ch, pos));
      i += 1;
      continue;
    }
    if (ch === ',') {
      tokens.push(mk('comma', ch, pos));
      i += 1;
      continue;
    }
    if (ch === '!') {
      tokens.push(mk('operator', ch, pos));
      i += 1;
      continue;
    }

    throw new SyntaxError(`unexpected character "${ch}" at pos ${pos}`, pos);
  }

  tokens.push(mk('eof', '', n));
  return tokens;
}

/* ----------------------------- helpers ----------------------------- */

function mk(kind: TokenKind, value: string, pos: number): Token {
  return { kind, value, pos };
}

function isDigit(c: string): boolean {
  return c >= '0' && c <= '9';
}

function isAlpha(c: string): boolean {
  return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
}

function isWhitespace(c: string): boolean {
  return c === ' ' || c === '\t' || c === '\n' || c === '\r';
}

function next(input: string, i: number): string {
  return input[i + 1] ?? '';
}
