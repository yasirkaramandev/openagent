/**
 * scientific.ts — scientific function registry honoring EvalContext.angleMode.
 *
 * Contract (architecture §2.5):
 *   - Angle mode is conveyed exclusively via ctx.angleMode ('DEG' | 'RAD').
 *   - trig inputs are normalised to radians internally; inverse results are
 *     converted back to the active mode before returning.
 *   - Affected: sin, cos, tan, asin, acos, atan.
 *   - Hyperbolic functions (sinh, cosh, tanh) are dimensionless and ignore
 *     angle mode.
 *
 * Each function takes already-evaluated numeric arguments and a context,
 * returning a number. The evaluator maps CallNode → registry entry and
 * enforces arity. Domain/overflow failures raise typed errors; this module
 * imports nothing from the DOM or React.
 */

import { DivisionByZero, DomainError, Overflow, type AngleMode } from './types';
import { factorial } from './decimal';

/** Deg→rad and rad→deg conversion factors. */
const DEG_TO_RAD = Math.PI / 180;

/** Convert an angle input to radians per the active mode. */
function toRadians(angle: number, mode: AngleMode): number {
  return mode === 'DEG' ? angle * DEG_TO_RAD : angle;
}

/** Convert a radian result back to the active mode (for inverse trig). */
function fromRadians(rad: number, mode: AngleMode): number {
  return mode === 'DEG' ? rad / DEG_TO_RAD : rad;
}

/**
 * Scientific function entry. `arity` lets the evaluator validate call sites;
 * `apply` performs the math and may throw typed errors.
 */
export interface ScientificFn {
  name: string;
  arity: number;
  apply: (args: number[], ctx: { angleMode: AngleMode }) => number;
}

/* ------------------------------------------------------------------ *
 * Individual implementations.
 * ------------------------------------------------------------------ */

function unaryArg(args: number[], fnName: string): number {
  const x = args[0];
  if (x === undefined) {
    throw new DomainError(`${fnName} requires one numeric argument`);
  }
  return x;
}

function sin(args: number[], ctx: { angleMode: AngleMode }): number {
  const x = unaryArg(args, 'sin');
  return Math.sin(toRadians(x, ctx.angleMode));
}
function cos(args: number[], ctx: { angleMode: AngleMode }): number {
  const x = unaryArg(args, 'cos');
  return Math.cos(toRadians(x, ctx.angleMode));
}
function tan(args: number[], ctx: { angleMode: AngleMode }): number {
  const x = unaryArg(args, 'tan');
  // tan(90deg) / tan(pi/2) is an asymptote; detect and fail closed.
  const r = toRadians(x, ctx.angleMode);
  if (Math.abs(Math.cos(r)) < 1e-15) {
    throw new DivisionByZero('tan(angle) undefined (asymptote)');
  }
  return Math.tan(r);
}

function asin(args: number[], ctx: { angleMode: AngleMode }): number {
  const x = unaryArg(args, 'asin');
  if (x < -1 || x > 1) {
    throw new DomainError(`asin domain is [-1, 1]; got ${x}`);
  }
  return fromRadians(Math.asin(x), ctx.angleMode);
}
function acos(args: number[], ctx: { angleMode: AngleMode }): number {
  const x = unaryArg(args, 'acos');
  if (x < -1 || x > 1) {
    throw new DomainError(`acos domain is [-1, 1]; got ${x}`);
  }
  return fromRadians(Math.acos(x), ctx.angleMode);
}
function atan(args: number[], ctx: { angleMode: AngleMode }): number {
  const x = unaryArg(args, 'atan');
  return fromRadians(Math.atan(x), ctx.angleMode);
}

/** Hyperbolic functions are dimensionless — angle mode ignored. */
function sinh(args: number[]): number {
  const x = unaryArg(args, 'sinh');
  return Math.sinh(x);
}
function cosh(args: number[]): number {
  const x = unaryArg(args, 'cosh');
  return Math.cosh(x);
}
function tanh(args: number[]): number {
  const x = unaryArg(args, 'tanh');
  return Math.tanh(x);
}

/** Base-10 logarithm; domain (0, +inf). */
function log10(args: number[]): number {
  const x = unaryArg(args, 'log10');
  if (x <= 0) throw new DomainError(`log10 domain is (0, +inf); got ${x}`);
  return Math.log10(x);
}

/** Natural logarithm; domain (0, +inf). */
function ln(args: number[]): number {
  const x = unaryArg(args, 'ln');
  if (x <= 0) throw new DomainError(`ln domain is (0, +inf); got ${x}`);
  return Math.log(x);
}

/** Square root; domain [0, +inf). */
function sqrt(args: number[]): number {
  const x = unaryArg(args, 'sqrt');
  if (x < 0) throw new DomainError(`sqrt domain is [0, +inf); got ${x}`);
  const r = Math.sqrt(x);
  if (!Number.isFinite(r)) throw new Overflow('sqrt overflow');
  return r;
}

/** Cube root; defined for all reals. */
function cbrt(args: number[]): number {
  const x = unaryArg(args, 'cbrt');
  const r = Math.cbrt(x);
  if (!Number.isFinite(r)) throw new Overflow('cbrt overflow');
  return r;
}

/* ------------------------------------------------------------------ *
 * Registry: name → implementation.
 * ------------------------------------------------------------------ */

const REGISTRY: Record<string, ScientificFn> = {
  sin: { name: 'sin', arity: 1, apply: sin },
  cos: { name: 'cos', arity: 1, apply: cos },
  tan: { name: 'tan', arity: 1, apply: tan },
  asin: { name: 'asin', arity: 1, apply: asin },
  acos: { name: 'acos', arity: 1, apply: acos },
  atan: { name: 'atan', arity: 1, apply: atan },
  sinh: { name: 'sinh', arity: 1, apply: sinh },
  cosh: { name: 'cosh', arity: 1, apply: cosh },
  tanh: { name: 'tanh', arity: 1, apply: tanh },
  log10: { name: 'log10', arity: 1, apply: log10 },
  ln: { name: 'ln', arity: 1, apply: ln },
  sqrt: { name: 'sqrt', arity: 1, apply: sqrt },
  cbrt: { name: 'cbrt', arity: 1, apply: cbrt },
};

/** Look up a registered scientific function by name, or undefined. */
export function getScientificFn(name: string): ScientificFn | undefined {
  return REGISTRY[name];
}

/** Named constants surfaced to the evaluator. */
export function constantValue(name: string): number | undefined {
  switch (name) {
    case 'pi':
      return Math.PI;
    case 'e':
      return Math.E;
    default:
      return undefined;
  }
}

export { factorial as factorialScientific };
export { factorial };
