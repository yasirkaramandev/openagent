/**
 * evaluator.ts — walk the AST to a Decimal result; format results to text.
 *
 * Contract (architecture §2.2):
 *   - evaluate(node, ctx): number — pure and total over well-formed nodes;
 *     runtime math failures raise typed errors (DivisionByZero, DomainError,
 *     Overflow). For binops + - * / we use integer-scaled decimal arithmetic
 *     so `0.1 + 0.2 === 0.3` exactly. Transcendentals use Math.* via the
 *     scientific registry and are promoted as-is.
 *   - format(value, opts): string — the ONLY place locale/grouping may be
 *     applied. Rounding honors opts.precision (significant digits).
 *
 * NumNode exactness: NumNode carries a numeric `value`, but for the exact
 * decimal core the evaluator re-derives a DecimalRep from the node's source
 * literal (exposed via numNodeSource) when present; otherwise it falls back
 * to numberToDecimal(value). This preserves exactness for literal inputs
 * while still working for computed/hyperbolic values that have no source.
 */

import { constantValue, getScientificFn } from './scientific';
import {
  decimalAdd,
  decimalDivide,
  decimalMultiply,
  decimalSubtract,
  numberToDecimal,
  parseDecimal,
} from './decimal';
import {
  DivisionByZero,
  DomainError,
  InvalidFactorial,
  Overflow,
  SyntaxError,
  type EvalContext,
  type FormatOptions,
  type Node,
  type NumNode,
} from './types';
import { numNodeSource } from './parser';
import { factorial } from './decimal';

/** Default context used when the caller omits fields. */
export const DEFAULT_CONTEXT: EvalContext = {
  angleMode: 'RAD',
  precision: 12,
  maxNodeDepth: 256,
};

/** Evaluate an AST under the given context, returning a canonical number. */
export function evaluate(
  node: Node,
  ctx: EvalContext = DEFAULT_CONTEXT,
): number {
  return evalNode(
    node,
    ctx,
    0,
    ctx.maxNodeDepth || DEFAULT_CONTEXT.maxNodeDepth,
  );
}

function evalNode(
  node: Node,
  ctx: EvalContext,
  depth: number,
  maxDepth: number,
): number {
  if (depth > maxDepth) {
    throw new Overflow('expression too deep (maxNodeDepth exceeded)');
  }
  switch (node.kind) {
    case 'num':
      return node.value;

    case 'constant': {
      const v = constantValue(node.name);
      if (v === undefined)
        throw new SyntaxError(`unknown constant "${node.name}"`);
      return v;
    }

    case 'unary': {
      const operand = evalNode(node.operand, ctx, depth + 1, maxDepth);
      if (node.op === '-') return -operand;
      throw new SyntaxError(`unknown unary operator "${node.op}"`);
    }

    case 'binop': {
      return evalBinop(node, ctx, depth, maxDepth);
    }

    case 'call': {
      const fn = getScientificFn(node.name);
      if (!fn) {
        throw new SyntaxError(`unknown function "${node.name}"`);
      }
      const args = node.args.map((a) => evalNode(a, ctx, depth + 1, maxDepth));
      if (args.length !== fn.arity) {
        throw new SyntaxError(
          `function "${node.name}" expects ${fn.arity} argument(s), got ${args.length}`,
        );
      }
      const r = fn.apply(args, { angleMode: ctx.angleMode });
      guardFinite(r, node.name);
      return r;
    }

    case 'factorial': {
      const op = evalNode(node.operand, ctx, depth + 1, maxDepth);
      try {
        return factorial(op);
      } catch (e) {
        // Re-throw the typed errors from decimal.factoial unchanged.
        if (e instanceof InvalidFactorial || e instanceof Overflow) throw e;
        throw new InvalidFactorial(`invalid factorial operand: ${op}`);
      }
    }

    default: {
      // Exhaustiveness guard.
      const _exhaustive: never = node;
      return _exhaustive;
    }
  }
}

/** Evaluate a binary operator, using exact decimal arithmetic for + - / * %. */
function evalBinop(
  node: Extract<Node, { kind: 'binop' }>,
  ctx: EvalContext,
  depth: number,
  maxDepth: number,
): number {
  const leftNode = node.left;
  const rightNode = node.right;
  // Compute operands as DecimalReps when possible to keep exactness.
  const lv = evalNode(leftNode, ctx, depth + 1, maxDepth);
  const rv = evalNode(rightNode, ctx, depth + 1, maxDepth);

  switch (node.op) {
    case '+':
      return decimalAdd(toRep(leftNode, lv), toRep(rightNode, rv));
    case '-':
      return decimalSubtract(toRep(leftNode, lv), toRep(rightNode, rv));
    case '*':
      return decimalMultiply(toRep(leftNode, lv), toRep(rightNode, rv));
    case '/':
      if (rv === 0) throw new DivisionByZero('division by zero');
      return decimalDivide(toRep(leftNode, lv), toRep(rightNode, rv));
    case '%': {
      if (rv === 0) throw new DivisionByZero('modulo by zero');
      // Remainder follows the sign of the dividend (JS % semantics). Both
      // operands are numbers here; we delegate to the JS remainder operator,
      // which already matches our desired dividend-sign convention.
      return lv % rv;
    }
    case '^': {
      return powOp(lv, rv, leftNode, rightNode);
    }
    default:
      throw new SyntaxError(`unknown operator "${node.op}"`);
  }
}

/**
 * Exponentiation. Integer exponents use Math.pow (exact for small bases);
 * fractional exponents enforce the domain rule for negative bases. A
 * negative base with a fractional exponent is a DomainError.
 */
function powOp(
  base: number,
  exp: number,
  baseNode: Node,
  expNode: Node,
): number {
  void baseNode;
  void expNode;
  if (base < 0 && !Number.isInteger(exp)) {
    throw new DomainError(
      `negative base with fractional exponent is undefined: (${base})^${exp}`,
    );
  }
  // 0^0 is defined as 1 by convention here.
  if (base === 0 && exp === 0) return 1;
  if (base === 0 && exp < 0)
    throw new DivisionByZero('0 raised to a negative power');
  const r = Math.pow(base, exp);
  guardFinite(r, '^');
  return r;
}

/** Pick a DecimalRep, preferring a NumNode's source literal for exactness. */
function toRep(
  node: Node,
  fallbackValue: number,
): ReturnType<typeof numberToDecimal> {
  if (node.kind === 'num') {
    const src = numNodeSource(node as NumNode);
    if (src !== undefined) return parseDecimal(src);
  }
  return numberToDecimal(fallbackValue);
}

/** Throw Overflow for non-finite results from transcendental/binary ops. */
function guardFinite(value: number, op: string): void {
  if (Number.isNaN(value)) {
    throw new DomainError(`operation "${op}" produced NaN`);
  }
  if (!Number.isFinite(value)) {
    throw new Overflow(`operation "${op}" produced an infinite result`);
  }
}

/* ------------------------------------------------------------------ *
 * format — the ONLY place locale/grouping may be applied.
 * ------------------------------------------------------------------ */

/** Format a numeric value to a display string honoring precision & notation. */
export function format(value: number, opts?: Partial<FormatOptions>): string {
  const precision = opts?.precision ?? 12;
  const notation = opts?.notation ?? 'auto';
  const decimalSeparator = opts?.decimalSeparator ?? '.';
  const groupSeparator = opts?.groupSeparator ?? '';

  if (Number.isNaN(value)) return 'NaN';
  if (!Number.isFinite(value)) {
    return value > 0 ? 'Infinity' : '-Infinity';
  }
  if (value === 0) return applySep('0', decimalSeparator, groupSeparator);

  // Choose a raw numeric string per notation, then round to significant digits.
  let raw: string;
  if (notation === 'fixed') {
    raw = value.toFixed(Math.max(0, precision - 1));
  } else if (notation === 'scientific') {
    raw = value.toExponential(Math.max(0, precision - 1));
  } else {
    // 'auto': keep significant digits, prefer fixed unless order too large.
    const order = Math.floor(Math.log10(Math.abs(value)));
    if (order >= -4 && order < precision) {
      const frac = Math.max(0, precision - 1 - order);
      raw = roundToSignificant(value, precision, frac);
    } else {
      raw = value.toExponential(Math.max(0, precision - 1));
    }
  }

  // Trim trailing zeros after a decimal point for auto/fixed prettiness.
  if (notation !== 'scientific' && raw.includes('.') && !/[eE]/.test(raw)) {
    raw = raw.replace(/0+$/, '').replace(/\.$/, '');
  }
  return applySep(raw, decimalSeparator, groupSeparator);
}

/** Round to `sig` significant digits, then render with `frac` fractional digits. */
function roundToSignificant(value: number, sig: number, frac: number): string {
  void sig;
  return value.toFixed(frac);
}

/**
 * Apply the chosen decimal separator and optional thousands grouping to an
 * already-canonical dot-decimal string. Exponential notation passes through.
 */
function applySep(
  raw: string,
  decimalSeparator: '.' | ',',
  groupSeparator: string,
): string {
  if (/[eE]/.test(raw)) {
    return raw; // scientific form bypasses grouping
  }
  const [rawIntPart = '', fracPart = ''] = raw.split('.');
  let intPart = rawIntPart;
  let sign = '';
  if (intPart.startsWith('-')) {
    sign = '-';
    intPart = intPart.slice(1);
  }
  if (groupSeparator !== '') {
    intPart = groupInteger(intPart, groupSeparator);
  }
  const withDec = fracPart === '' ? intPart : `${intPart}.${fracPart}`;
  const result = decimalSeparator === ',' ? withDec.replace('.', ',') : withDec;
  return `${sign}${result}`;
}

/** Insert group separators every 3 digits from the right. */
function groupInteger(intPart: string, sep: string): string {
  if (intPart === '0') return intPart;
  let out = '';
  for (let i = 0; i < intPart.length; i++) {
    if (i > 0 && (intPart.length - i) % 3 === 0) out += sep;
    out += intPart[i] ?? '';
  }
  return out;
}
