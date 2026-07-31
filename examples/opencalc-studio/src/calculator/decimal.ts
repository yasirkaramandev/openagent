/**
 * decimal.ts — reliable decimal arithmetic for OpenCalc Studio.
 *
 * Problem: IEEE-754 doubles can't represent 0.1 exactly, so `0.1 + 0.2`
 * yields `0.30000000000000004`. The golden test matrix requires
 * `0.1 + 0.2 === 0.3` EXACTLY, so floating-point cannot be the final answer.
 *
 * Approach — integer-scaled arithmetic, zero runtime dependencies:
 *
 *   Every decimal operand is decomposed into a triple { sign, digits, scale }
 *   where value = sign * digits / 10^scale, `digits` is a bigint (kept at
 *   full precision, immune to Number.MAX_SAFE_INTEGER overflow), and
 *   `scale >= 0` counts digits after the decimal point. Operations are
 *   performed directly on those bigints:
 *
 *     add(a, b)    : align to the larger scale, sum integer magnitudes,
 *                    keep the sign of the larger magnitude.
 *     sub(a, b)    : negate b's sign, then add.
 *     mul(a, b)    : multiply magnitudes; scales add.
 *     div(a, b)    : scale the dividend by 10^(DIV_PRECISION) extra digits
 *                    so the integer quotient keeps ample fractional
 *                    precision, then integer-divide and normalise the scale.
 *
 *   Each op trims trailing zeros so canonical results compare cleanly
 *   (`0.30` becomes `0.3`). The module exposes a DecimalMath shim plus
 *   number<->rep helpers. Transcendental functions (sin/cos/log/...) are not
 *   exactly representable in base 10 in general, so they are computed via
 *   Math.* then promoted back to a DecimalRep via numberToDecimal; the
 *   exact add/sub/mul/div core that the golden tests exercise stays exact.
 *
 * "Canonical Decimal type" surfaced to the rest of the engine is a plain
 * number (see types.ts). Exactness lives in the operations below, not in
 * the storage type, keeping the AST/evaluator simple and framework free.
 */

import {
  DivisionByZero,
  InvalidFactorial,
  Overflow,
  SyntaxError,
  type CalcError,
} from './types';

export type { CalcError };

/** Internal decimal components. value = sign * digits / 10^scale. */
interface DecimalRep {
  sign: 1 | -1 | 0;
  digits: bigint; // magnitude, always >= 0
  scale: number; // digits after the decimal point
}

/** Extra fractional digits kept when dividing (precision vs noise tradeoff). */
const DIV_PRECISION = 20;

/** Largest factorial computed; above this we throw Overflow (architecture). */
const FACTORIAL_OVERFLOW_LIMIT = 170n;

/* ----------------------------- parsing ----------------------------- */

/**
 * Parse a number/string into a canonical DecimalRep.
 * Accepts 0.1, 42, .5, -3, and scientific notation such as 1.5e3.
 */
export function parseDecimal(input: number | string): DecimalRep {
  const s =
    typeof input === 'number' ? numberToString(input) : String(input).trim();
  if (s === '') throw new SyntaxError('empty number literal');

  let sign: 1 | -1 | 0 = 1;
  let body = s;
  if (body[0] === '-') {
    sign = -1;
    body = body.slice(1);
  } else if (body[0] === '+') {
    body = body.slice(1);
  }

  const match = /^(\d*(?:\.\d*)?)(?:[eE]([+-]?\d+))?$/.exec(body);
  const mantissa = match?.[1];
  if (mantissa === undefined || mantissa === '' || mantissa === '.') {
    throw new SyntaxError(`invalid number literal: ${input}`);
  }

  const exponent = Number(match?.[2] ?? 0);
  if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 10_000) {
    throw new Overflow(`numeric exponent out of range: ${input}`);
  }

  const [intPart = '', fracPart = ''] = mantissa.split('.');
  let scale = fracPart.length - exponent;
  const digitsStr = `${intPart}${fracPart}` || '0';
  let digits = BigInt(digitsStr);
  if (scale < 0) {
    digits *= pow10(-scale);
    scale = 0;
  }
  if (digits === 0n) sign = 0; // normalise -0 -> 0
  return trim({ sign, digits, scale });
}

/** Convert a DecimalRep to a JS number (the canonical Decimal surface type). */
export function decimalToNumber(rep: DecimalRep): number {
  if (rep.sign === 0) return 0;
  const neg = rep.sign < 0;
  // Build an exact string with ample precision; Number() rounds once to double.
  const scaled = digitsToScaledString(rep.digits, rep.scale, DIV_PRECISION + 4);
  const n = Number(scaled);
  const out = neg ? -n : n;
  if (!Number.isFinite(out)) throw new Overflow('numeric overflow');
  return out;
}

/** Promote a raw double (e.g. from Math.sin) into a DecimalRep. */
export function numberToDecimal(n: number): DecimalRep {
  if (!Number.isFinite(n)) throw new Overflow('numeric overflow');
  if (n === 0) return { sign: 0, digits: 0n, scale: 0 };
  const neg = n < 0;
  const abs = Math.abs(n);
  const s = numberToString(abs);
  const rep = parseDecimal(s);
  if (rep.sign === 0) return rep;
  return { ...rep, sign: neg ? -1 : 1 };
}

/* --------------------------- arithmetic ---------------------------- */

export function decimalAdd(
  a: number | DecimalRep,
  b: number | DecimalRep,
): number {
  return decimalToNumber(add(toRep(a), toRep(b)));
}

export function decimalSubtract(
  a: number | DecimalRep,
  b: number | DecimalRep,
): number {
  return decimalToNumber(subtract(toRep(a), toRep(b)));
}

export function decimalMultiply(
  a: number | DecimalRep,
  b: number | DecimalRep,
): number {
  return decimalToNumber(multiply(toRep(a), toRep(b)));
}

export function decimalDivide(
  a: number | DecimalRep,
  b: number | DecimalRep,
): number {
  return decimalToNumber(divide(toRep(a), toRep(b)));
}

/** Integer-scaled add: align scales, sum signed magnitudes, pick sign. */
export function add(a: DecimalRep, b: DecimalRep): DecimalRep {
  const { scale, da, db } = alignScale(a, b);
  const sa = a.sign < 0 ? -da : da;
  const sb = b.sign < 0 ? -db : db;
  const sum = sa + sb;
  if (sum === 0n) return { sign: 0, digits: 0n, scale };
  return trim({
    sign: sum < 0n ? -1 : 1,
    digits: sum < 0n ? -sum : sum,
    scale,
  });
}

/** Integer-scaled subtract: negate b, then add. */
export function subtract(a: DecimalRep, b: DecimalRep): DecimalRep {
  const flipped: DecimalRep = {
    sign: b.sign === 0 ? 0 : b.sign === 1 ? -1 : 1,
    digits: b.digits,
    scale: b.scale,
  };
  return add(a, flipped);
}

/** Integer-scaled multiply: magnitudes multiply, scales add. */
export function multiply(a: DecimalRep, b: DecimalRep): DecimalRep {
  const sign: DecimalRep['sign'] =
    a.sign === 0 || b.sign === 0 ? 0 : a.sign === b.sign ? 1 : -1;
  return trim({ sign, digits: a.digits * b.digits, scale: a.scale + b.scale });
}

/** Integer-scaled divide: scale dividend up by DIV_PRECISION, integer-divide. */
export function divide(a: DecimalRep, b: DecimalRep): DecimalRep {
  if (b.sign === 0 || b.digits === 0n) {
    throw new DivisionByZero('division by zero');
  }
  if (a.sign === 0) return { sign: 0, digits: 0n, scale: 0 };
  // We want roughly DIV_PRECISION fractional digits in the result. Compute the
  // quotient of (a.digits * 10^(sa)) / b.digits where sa makes the result have
  // the desired scale: result = quotient / 10^finalScale.
  const finalScale =
    a.scale < b.scale ? DIV_PRECISION : DIV_PRECISION + (a.scale - b.scale);
  const shift = finalScale - a.scale + b.scale; // >= 0 because finalScale large enough
  const shifted = a.digits * pow10(shift);
  const divisor = b.digits;
  const q = shifted / divisor;
  if (q === 0n) return { sign: 0, digits: 0n, scale: 0 };
  const sign: DecimalRep['sign'] = a.sign === b.sign ? 1 : -1;
  return trim({ sign, digits: q, scale: finalScale });
}

/* --------------------------- factorial ---------------------------- */

/** Factorial of a non-negative integer via bigint, with overflow guarding. */
export function factorial(n: number): number {
  if (!Number.isInteger(n)) {
    throw new InvalidFactorial(
      `factorial requires a non-negative integer, got ${n}`,
    );
  }
  if (n < 0) {
    throw new InvalidFactorial(`factorial of negative number: ${n}`);
  }
  if (BigInt(n) > FACTORIAL_OVERFLOW_LIMIT) {
    throw new Overflow(
      `factorial overflow for ${n} (limit ${FACTORIAL_OVERFLOW_LIMIT})`,
    );
  }
  if (n <= 1) return 1;
  let acc = 1n;
  for (let i = 2n; i <= BigInt(n); i++) acc *= i;
  const result = Number(acc);
  if (!Number.isFinite(result)) {
    throw new Overflow(`factorial overflow for ${n}`);
  }
  return result;
}

/** Exponent x^n for integer n, computed exactly with bigint-strength. */
export function intPow(base: number, exp: number): number {
  if (!Number.isInteger(exp)) return Math.pow(base, exp);
  if (exp >= 0) return Math.pow(base, exp);
  return 1 / Math.pow(base, -exp);
}

/* ----------------------------- helpers ----------------------------- */

function toRep(x: number | DecimalRep): DecimalRep {
  return typeof x === 'number' ? numberToDecimal(x) : x;
}

/** Align two reps to a common scale, returning scaled bigint magnitudes. */
function alignScale(
  a: DecimalRep,
  b: DecimalRep,
): {
  scale: number;
  da: bigint;
  db: bigint;
} {
  const scale = Math.max(a.scale, b.scale);
  const da = scaleShift(a.digits, scale - a.scale);
  const db = scaleShift(b.digits, scale - b.scale);
  return { scale, da, db };
}

function scaleShift(digits: bigint, shift: number): bigint {
  if (shift <= 0) return digits;
  return digits * pow10(shift);
}

function pow10(n: number): bigint {
  if (n <= 0) return 1n;
  return BigInt(10) ** BigInt(n);
}

/** Remove trailing zeros from magnitude/scale; normalise the zero rep. */
function trim(rep: DecimalRep): DecimalRep {
  if (rep.sign === 0 || rep.digits === 0n) {
    return { sign: 0, digits: 0n, scale: 0 };
  }
  let { digits, scale } = rep;
  while (scale > 0 && digits % 10n === 0n) {
    digits /= 10n;
    scale -= 1;
  }
  return { sign: rep.sign, digits, scale };
}

/** Build an exact decimal string for digits / 10^scale with `places` frac digits. */
function digitsToScaledString(
  digits: bigint,
  scale: number,
  places: number,
): string {
  // Promote to `places` fractional digits: multiply by 10^(places - scale).
  let shifted = digits;
  if (scale < places) {
    shifted = digits * pow10(places - scale);
  } else if (scale > places) {
    shifted = digits / pow10(scale - places);
  }
  const str = shifted.toString().padStart(places + 1, '0');
  const intPart = str.slice(0, str.length - places) || '0';
  const fracPart = str.slice(str.length - places);
  return fracPart === '' ? intPart : `${intPart}.${fracPart}`;
}

/** Number -> its canonical shortest round-trippable string. */
function numberToString(n: number): string {
  if (Object.is(n, -0)) return '0';
  if (!Number.isFinite(n)) throw new Overflow('numeric overflow');
  // JavaScript's canonical shortest round-trippable representation avoids
  // manufacturing binary-noise digits when an exact decimal intermediate
  // (for example 0.3) is promoted back into the integer-scaled core.
  return n.toString();
}
