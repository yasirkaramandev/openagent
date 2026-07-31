/**
 * engine.test.ts — Vitest golden matrix for the OpenCalc Studio engine.
 *
 * Covers the contract behaviours required by the task and architecture:
 *   - reliable decimal arithmetic (0.1 + 0.2 === 0.3)
 *   - operator precedence & grouping
 *   - scientific functions with DEG/RAD
 *   - factorial
 *   - exponentiation
 *   - fail-closed typed errors (DivisionByZero, DomainError, others)
 */

import { describe, expect, it } from 'vitest';
import { compute, tokenize, parse, evaluate, format } from './index';
import {
  DivisionByZero,
  DomainError,
  CalcError,
  SyntaxError,
  InvalidFactorial,
  MismatchedParens,
} from './types';

describe('OpenCalc Studio engine', () => {
  /* ----------------------- reliable decimal ----------------------- */
  describe('reliable decimal arithmetic', () => {
    it('0.1 + 0.2 === 0.3 exactly', () => {
      expect(compute('0.1 + 0.2').value).toBe(0.3);
      expect(compute('0.1 + 0.2 - 0.3').value).toBe(0);
    });

    it('0.3 - 0.1 === 0.2 exactly', () => {
      expect(compute('0.3 - 0.1').value).toBe(0.2);
    });

    it('0.1 * 3 === 0.3 exactly', () => {
      expect(compute('0.1 * 3').value).toBe(0.3);
    });

    it('0.3 / 0.1 === 3 exactly', () => {
      expect(compute('0.3 / 0.1').value).toBe(3);
    });

    it('large integer arithmetic stays exact', () => {
      const expected = Number(9_007_199_254_740_995n);
      expect(compute('9007199254740993 + 2').value).toBe(expected);
      expect(compute('1e-5 + 1').value).toBe(1.00001);
    });
  });

  /* ----------------------- precedence & grouping ----------------- */
  describe('operator precedence', () => {
    it('2 + 3 * 4 = 14', () => {
      expect(compute('2 + 3 * 4').value).toBe(14);
    });

    it('(2 + 3) * 4 = 20', () => {
      expect(compute('(2 + 3) * 4').value).toBe(20);
    });

    it('exponentiation is right-associative: 2^3^2 = 512', () => {
      expect(compute('2^3^2').value).toBe(512);
    });

    it('unary minus before exponent: -2^2 = -(2^2) = -4', () => {
      expect(compute('-2^2').value).toBe(-4);
    });

    it('2^-1 = 0.5', () => {
      expect(compute('2^-1').value).toBe(0.5);
    });

    it('modulo: 10 % 3 = 1', () => {
      expect(compute('10 % 3').value).toBe(1);
    });
  });

  /* --------------------- implicit multiplication ----------------- */
  describe('implicit multiplication', () => {
    it('2(3+1) = 8', () => {
      expect(compute('2(3+1)').value).toBe(8);
    });
    it('2pi integrates over the constant', () => {
      expect(compute('2pi').value).toBeCloseTo(2 * Math.PI, 12);
    });
    it('(2)(3) = 6', () => {
      expect(compute('(2)(3)').value).toBe(6);
    });
  });

  /* ----------------------- scientific ---------------------------- */
  describe('scientific functions', () => {
    it('sqrt(144) = 12', () => {
      expect(compute('sqrt(144)').value).toBe(12);
    });

    it('sin(30) in DEG ~ 0.5', () => {
      const { value } = compute('sin(30)', { angleMode: 'DEG' });
      expect(value).toBeCloseTo(0.5, 10);
    });

    it('cos(0) in RAD = 1', () => {
      expect(compute('cos(0)', { angleMode: 'RAD' }).value).toBe(1);
    });

    it('sin(30) in RAD differs from DEG', () => {
      const { value } = compute('sin(30)', { angleMode: 'RAD' });
      expect(value).toBeCloseTo(Math.sin(30), 12);
    });

    it('asin(1) in DEG = 90', () => {
      expect(compute('asin(1)', { angleMode: 'DEG' }).value).toBeCloseTo(
        90,
        10,
      );
    });

    it('log10(1000) = 3', () => {
      expect(compute('log10(1000)').value).toBeCloseTo(3, 12);
    });

    it('ln(e) = 1', () => {
      expect(compute('ln(e)').value).toBeCloseTo(1, 12);
    });

    it('cbrt(-8) = -2 (odd roots accept negatives)', () => {
      expect(compute('cbrt(-8)').value).toBe(-2);
    });

    it('factorial: 5! = 120', () => {
      expect(compute('5!').value).toBe(120);
    });

    it('0! = 1', () => {
      expect(compute('0!').value).toBe(1);
    });

    it('2^10 = 1024', () => {
      expect(compute('2^10').value).toBe(1024);
    });
  });

  /* ----------------------- fail-closed errors ------------------- */
  describe('typed errors (fail-closed)', () => {
    it('1/0 throws DivisionByZero', () => {
      expect(() => compute('1/0')).toThrow(DivisionByZero);
      expect(() => compute('1/0')).toThrow(CalcError);
    });

    it('modulo by zero throws DivisionByZero', () => {
      expect(() => compute('5 % 0')).toThrow(DivisionByZero);
    });

    it('sqrt(-1) throws DomainError', () => {
      expect(() => compute('sqrt(-1)')).toThrow(DomainError);
    });

    it('ln(0) throws DomainError', () => {
      expect(() => compute('ln(0)')).toThrow(DomainError);
    });

    it('ln(-5) throws DomainError', () => {
      expect(() => compute('ln(-5)')).toThrow(DomainError);
    });

    it('log10(-1) throws DomainError', () => {
      expect(() => compute('log10(-1)')).toThrow(DomainError);
    });

    it('asin(2) throws DomainError', () => {
      expect(() => compute('asin(2)', { angleMode: 'DEG' })).toThrow(
        DomainError,
      );
    });

    it('0.5! throws InvalidFactorial', () => {
      expect(() => compute('0.5!')).toThrow(InvalidFactorial);
    });

    it('(-3)! throws InvalidFactorial', () => {
      expect(() => compute('(-3)!')).toThrow(InvalidFactorial);
    });

    it('unbalanced "(" throws MismatchedParens', () => {
      expect(() => compute('(2+3')).toThrow(MismatchedParens);
    });

    it('unbalanced ")" throws MismatchedParens', () => {
      expect(() => compute('2+3)')).toThrow(MismatchedParens);
    });

    it('dangling operator throws SyntaxError', () => {
      expect(() => compute('2 +')).toThrow(SyntaxError);
      expect(() => compute('1..2')).toThrow(SyntaxError);
    });

    it('unknown identifier throws SyntaxError', () => {
      expect(() => compute('foo(1)')).toThrow(SyntaxError);
    });

    it('error.kind discriminates types', () => {
      try {
        compute('1/0');
        throw new Error('expected throw');
      } catch (e) {
        expect(e instanceof DivisionByZero).toBe(true);
        expect((e as CalcError).kind).toBe('DivisionByZero');
        expect((e as CalcError).code).toBe('DivisionByZero');
      }
    });
  });

  /* ----------------------- pipeline & format ------------------- */
  describe('pipeline and formatting', () => {
    it('tokenize/parse/evaluate compose correctly', () => {
      const v = evaluate(parse(tokenize('3 * (1 + 2)')), {
        angleMode: 'RAD',
        precision: 12,
        maxNodeDepth: 256,
      });
      expect(v).toBe(9);
    });

    it('compute returns both value and formatted', () => {
      const r = compute('2 + 2');
      expect(r.value).toBe(4);
      expect(r.formatted).toBe('4');
    });

    it('format respects precision', () => {
      const s = format(1 / 3, { precision: 4 });
      expect(s).toBe('0.3333');
    });

    it('format applies thousands grouping when requested', () => {
      const s = format(1234567, { precision: 7, groupSeparator: ',' });
      expect(s).toBe('1,234,567');
    });

    it('format honors comma decimal separator', () => {
      const s = format(1.5, { precision: 4, decimalSeparator: ',' });
      expect(s).toBe('1,5');
    });
  });
});
