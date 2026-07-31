/**
 * index.ts — public engine barrel.
 *
 * Exposes the pure pipeline:
 *
 *   tokenize → parse → evaluate → format
 *
 * plus a convenience `compute` that runs the whole pipeline in one call and
 * returns `{ value, formatted }`. The engine has zero React/DOM dependencies
 * and is safe to run from tests, Web Workers, or the UI adapter.
 */

import { evaluate, format, DEFAULT_CONTEXT } from './evaluator';
import { parse } from './parser';
import { tokenize } from './tokenizer';
import { type EvalContext, type FormatOptions } from './types';

export type { EvalContext, FormatOptions };

export { tokenize, parse, evaluate, format, DEFAULT_CONTEXT };
export {
  CalcError,
  DivisionByZero,
  DomainError,
  Overflow,
  SyntaxError,
  InvalidFactorial as InvalidFactorial,
  MismatchedParens as MismatchedParens,
  type AngleMode,
  type CalcErrorCode,
  type Token,
  type Node,
} from './types';
export {
  decimalAdd,
  decimalSubtract,
  decimalMultiply,
  decimalDivide,
  factorial,
} from './decimal';
export { getScientificFn, constantValue } from './scientific';

/** Result of a full compute pass. */
export interface ComputeResult {
  value: number;
  formatted: string;
}

/**
 * Run the full pipeline for an expression string.
 *
 * @param expression raw input, e.g. "0.1+0.2", "sin(30)", "5!"
 * @param ctx       optional EvalContext; missing fields fall back to defaults
 *                  (angleMode 'RAD', precision 12, maxNodeDepth 256).
 * @returns `{ value, formatted }` where formatted honors ctx.precision.
 */
export function compute(
  expression: string,
  ctx?: Partial<EvalContext>,
): ComputeResult {
  const fullCtx: EvalContext = {
    angleMode: ctx?.angleMode ?? DEFAULT_CONTEXT.angleMode,
    precision: ctx?.precision ?? DEFAULT_CONTEXT.precision,
    maxNodeDepth: ctx?.maxNodeDepth ?? DEFAULT_CONTEXT.maxNodeDepth,
  };
  const tokens = tokenize(expression);
  const ast = parse(tokens);
  const value = evaluate(ast, fullCtx);
  const formatted = format(value, { precision: fullCtx.precision });
  return { value, formatted };
}
