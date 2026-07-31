/**
 * types.ts — Shared engine types and typed error classes.
 *
 * Contract: https://.../docs/architecture.md §2.3, §2.4
 *
 * This module contains NO logic. It declares:
 *   - Token / Node shapes used by the tokenizer & parser.
 *   - EvalContext (angle mode + precision + depth guard) carried through
 *     evaluate(); the engine never reads global mutable state.
 *   - The `CalcError` base class plus one concrete subclass per kind. Every
 *     error carries a discriminating `code` (a.k.a. `kind`) the UI adapter can
 *     switch on to render a localized, fail-closed message.
 *
 * Division-of-labour note: the architecture document describes a `Decimal`
 * interface and a `format(value: Decimal,...)` surface. The OpenCalc engine
 * exposes a number-based decimal shim (see decimal.ts) whose canonical type
 * alias is `Decimal = number` so that the AST and signatures stay framework
 * free while intermediate add/subtract/multiply/divide use integer-scaled
 * arithmetic for exact results (0.1 + 0.2 === 0.3). `format` owns all rounding.
 */

/** Angle mode. DEG normalises trig inputs to radians; RAD uses them directly. */
export type AngleMode = 'DEG' | 'RAD';

/**
 * Configuration carried into every `evaluate` call. None of these are read
 * from a singleton (see architecture §"No global mutable state").
 *
 * @property angleMode  - how trig arguments/results are interpreted.
 * @property precision  - significant digits applied ONLY at `format` time.
 * @property maxNodeDepth - guard against pathologically deep AST input.
 */
export interface EvalContext {
  angleMode: AngleMode;
  precision: number;
  maxNodeDepth: number;
}

/** Discriminated-union member label for every error subclass. */
export type CalcErrorCode =
  | 'DivisionByZero'
  | 'DomainError'
  | 'Overflow'
  | 'SyntaxError'
  | 'InvalidFactorial'
  | 'MismatchedParens';

/**
 * Base class for every engine failure. Carries a discriminating `code`
 * plus an optional source position (`pos`) so the tokenizer/parser can
 * pinpoint the offending character. `kind` is an alias for `code` for
 * ergonomic `error.kind === '...'` checks.
 */
export class CalcError extends Error {
  readonly code: CalcErrorCode = 'SyntaxError';
  /** Alias for `code` — friendlier for callers (`err.kind`). */
  get kind(): CalcErrorCode {
    return this.code;
  }
  /** Optional 0-based character index of the offending input. */
  pos?: number;

  constructor(message: string, pos?: number) {
    super(message);
    this.name = this.constructor.name;
    if (typeof pos === 'number') this.pos = pos;
    // Restore prototype chain (目标的) — TS classes extending Error need this
    // when targeting older runtimes so `instanceof` keeps working.
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/** Division by zero: `1/0`, modulo by zero. */
export class DivisionByZero extends CalcError {
  readonly code = 'DivisionByZero' as const;
  constructor(message = 'division by zero', pos?: number) {
    super(message, pos);
  }
}

/** Math domain error: `sqrt(-1)`, `ln(<=0)`, `asin(2)`, etc. */
export class DomainError extends CalcError {
  readonly code = 'DomainError' as const;
  constructor(message = 'math domain error', pos?: number) {
    super(message, pos);
  }
}

/** Result too large to represent: `10^9999`, factorial of a huge integer. */
export class Overflow extends CalcError {
  readonly code = 'Overflow' as const;
  constructor(message = 'numeric overflow', pos?: number) {
    super(message, pos);
  }
}

/** Malformed input: bad number, dangling operator, unknown identifier. */
export class SyntaxError extends CalcError {
  readonly code = 'SyntaxError' as const;
  constructor(message = 'syntax error', pos?: number) {
    super(message, pos);
  }
}

/** Invalid factorial operand: negative, or non-integer (`0.5!`). */
export class InvalidFactorial extends CalcError {
  readonly code = 'InvalidFactorial' as const;
  constructor(message = 'invalid factorial operand', pos?: number) {
    super(message, pos);
  }
}

/** Unbalanced `(` or `)`. */
export class MismatchedParens extends CalcError {
  readonly code = 'MismatchedParens' as const;
  constructor(message = 'mismatched parentheses', pos?: number) {
    super(message, pos);
  }
}

/* ------------------------------------------------------------------ *
 * Token & Node shapes (architecture §2.3)
 * ------------------------------------------------------------------ */

/**
 * `Decimal` is the engine's canonical numeric type. The decimal shim
 * (decimal.ts) implements add/subtract/multiply/divide with integer-scaled
 * arithmetic so `0.1 + 0.2 === 0.3` exactly; surfaced values are plain
 * JS numbers used only after promotion from the integer-scaled core.
 */
export type Decimal = number;

/** Kind label for a token. */
export type TokenKind =
  | 'number'
  | 'operator'
  | 'lparen'
  | 'rparen'
  | 'comma'
  | 'ident'
  | 'constant'
  | 'eof';

/** A single lexical token. `pos` is the 0-based index into the source. */
export interface Token {
  kind: TokenKind;
  value: string;
  pos: number;
}

/** Discriminator label for an AST node. */
export type NodeKind =
  'num' | 'binop' | 'unary' | 'call' | 'constant' | 'factorial';

/** Numeric literal. */
export interface NumNode {
  kind: 'num';
  value: Decimal;
}

/** Binary operator application: + - * / % ^. */
export interface BinopNode {
  kind: 'binop';
  op: string;
  left: Node;
  right: Node;
}

/** Unary operator application, currently only prefix `-` (negative). */
export interface UnaryNode {
  kind: 'unary';
  op: string;
  operand: Node;
}

/** Function call, e.g. `sin(x)`, `log10(x)`, `pow(x, y)` if extended. */
export interface CallNode {
  kind: 'call';
  name: string;
  args: Node[];
}

/** Named constant: `pi`, `e`. */
export interface ConstantNode {
  kind: 'constant';
  name: string;
}

/** Postfix factorial: `5!`, `(n+1)!`. */
export interface FactorialNode {
  kind: 'factorial';
  operand: Node;
}

/** Root AST node union. */
export type Node =
  NumNode | BinopNode | UnaryNode | CallNode | ConstantNode | FactorialNode;

/** Options consumed by `format`. Only here may locale be applied. */
export interface FormatOptions {
  precision: number;
  notation: 'auto' | 'fixed' | 'scientific';
  groupSeparator?: string;
  decimalSeparator: '.' | ',';
}
