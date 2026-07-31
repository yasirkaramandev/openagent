/**
 * parser.ts — convert a Token stream into an evaluation AST.
 *
 * Contract (architecture §2.2): `parse` produces a valid Node tree or
 * throws. Grammar, arity, and balanced parentheses are enforced here.
 *
 *   expression := addSub
 *   addSub := mulDiv (('+' | '-') mulDiv)*            // lowest precedence
 *   mulDiv := implicitMul (('*' | '/' | '%') implicitMul)*
 *   implicitMul := unary (unaryStart)*                // implicit x: 2(3), 2pi
 *   unary := ('-' | '+')* power
 *   power := postfix ('^' unary)*                     // right-assoc exponent
 *   postfix := primary ('!')*                          // postfix factorial
 *   primary := number
 *            | constant                                 // pi, e
 *            | ident '(' args ')'                       // function call
 *            | '(' expression ')'                        // grouping
 *
 * Implicit multiplication: `2(3+1)`, `2pi`, `(2)(3)`, `2sin(30)` are legal.
 * A value-producing node immediately followed by another value start becomes
 * an implicit `*`.
 *
 * NumNode exactness: the node stores the canonical Decimal value (a number)
 * AND the original literal string (via the private `_source` field) so the
 * evaluator can re-derive an exact DecimalRep through `parseDecimal` for the
 * add/sub/mul/div core, giving `0.1 + 0.2 === 0.3`.
 */

import {
  MismatchedParens,
  SyntaxError,
  type Node,
  type NumNode,
  type Token,
} from './types';
import { decimalToNumber, parseDecimal } from './decimal';

/** Unbracket a NumNode's optional source-literal entry hidden from the type. */
const SOURCE = Symbol('opencalc.source literal');

export interface NumNodeWithSource extends NumNode {
  [SOURCE]?: string;
}

export function numNodeSource(node: NumNode): string | undefined {
  return (node as NumNodeWithSource)[SOURCE];
}

/** Parse a token stream (terminated by eof) into an AST. */
export function parse(tokens: Token[]): Node {
  const parser = new Parser(tokens);
  const node = parser.parseExpression();
  const eof = parser.peek();
  if (eof.kind !== 'eof') {
    if (eof.kind === 'rparen') {
      throw new MismatchedParens('unbalanced ")"', eof.pos);
    }
    throw new SyntaxError(`unexpected token "${eof.value}"`, eof.pos);
  }
  return node;
}

function mkBinop(op: string, left: Node, right: Node): Node {
  return { kind: 'binop', op, left, right };
}

class Parser {
  private readonly tokens: Token[];
  private i = 0;

  constructor(tokens: Token[]) {
    this.tokens = tokens;
  }

  parseExpression(): Node {
    return this.parseAddSub();
  }

  private parseAddSub(): Node {
    let left = this.parseMulDiv();
    while (this.isOp('+') || this.isOp('-')) {
      const op = this.advance().value;
      const right = this.parseMulDiv();
      left = mkBinop(op, left, right);
    }
    return left;
  }

  private parseMulDiv(): Node {
    let left = this.parseImplicitMul();
    while (this.isOp('*') || this.isOp('/') || this.isOp('%')) {
      const op = this.advance().value;
      const right = this.parseImplicitMul();
      left = mkBinop(op, left, right);
    }
    return left;
  }

  /** Implicit multiplication between adjacent value-producing nodes. */
  private parseImplicitMul(): Node {
    let left = this.parseUnary();
    while (this.startsValue()) {
      const right = this.parseUnary();
      left = mkBinop('*', left, right);
    }
    return left;
  }

  private parseUnary(): Node {
    if (this.isOp('-')) {
      this.advance();
      const operand = this.parseUnary();
      return { kind: 'unary', op: '-', operand };
    }
    if (this.isOp('+')) {
      this.advance(); // unary plus is a no-op
      return this.parseUnary();
    }
    return this.parsePower();
  }

  /** Right-associative exponentiation: 2^3^2 -> 2^(3^2). */
  private parsePower(): Node {
    const base = this.parsePostfix();
    if (this.isOp('^')) {
      this.advance();
      const exponent = this.parseUnary(); // allows 2^-1
      return mkBinop('^', base, exponent);
    }
    return base;
  }

  /** Postfix factorial: 5!, (2+3)!, repeated: 5!! . */
  private parsePostfix(): Node {
    let node = this.parsePrimary();
    while (this.isOp('!')) {
      this.advance();
      node = { kind: 'factorial', operand: node };
    }
    return node;
  }

  private parsePrimary(): Node {
    const tok = this.peek();
    switch (tok.kind) {
      case 'number': {
        this.advance();
        const rep = parseDecimal(tok.value);
        const value = decimalToNumber(rep);
        const node: NumNode = { kind: 'num', value };
        (node as NumNodeWithSource)[SOURCE] = tok.value;
        return node;
      }
      case 'constant': {
        this.advance();
        return { kind: 'constant', name: tok.value };
      }
      case 'ident': {
        const nameTok = this.advance();
        const lp = this.peek();
        if (lp.kind !== 'lparen') {
          throw new SyntaxError(
            `expected '(' after function "${nameTok.value}"`,
            lp.pos,
          );
        }
        this.advance();
        const args: Node[] = [];
        if (this.peek().kind !== 'rparen') {
          args.push(this.parseExpression());
          while (this.isComma()) {
            this.advance();
            args.push(this.parseExpression());
          }
        }
        const rp = this.peek();
        if (rp.kind !== 'rparen') {
          if (rp.kind === 'eof') {
            throw new MismatchedParens('unbalanced "(" — missing ")"', lp.pos);
          }
          throw new MismatchedParens('expected ")"', rp.pos);
        }
        this.advance();
        return { kind: 'call', name: nameTok.value, args };
      }
      case 'lparen': {
        this.advance();
        const inner = this.parseExpression();
        const rp = this.peek();
        if (rp.kind !== 'rparen') {
          if (rp.kind === 'eof') {
            throw new MismatchedParens('unbalanced "(" — missing ")"', tok.pos);
          }
          throw new MismatchedParens('expected ")"', rp.pos);
        }
        this.advance();
        return inner;
      }
      case 'eof':
        throw new SyntaxError('unexpected end of input', tok.pos);
      default:
        throw new SyntaxError(`unexpected token "${tok.value}"`, tok.pos);
    }
  }

  /* ----------------------- lookahead helpers ------------------------ */

  peek(): Token {
    const t = this.tokens[this.i];
    if (t === undefined) {
      return { kind: 'eof', value: '', pos: -1 };
    }
    return t;
  }

  private advance(): Token {
    const t = this.peek();
    this.i += 1;
    return t;
  }

  private isOp(op: string): boolean {
    const t = this.peek();
    return t.kind === 'operator' && t.value === op;
  }

  private isComma(): boolean {
    return this.peek().kind === 'comma';
  }

  /** True when the upcoming token begins a value (for implicit multiplication). */
  private startsValue(): boolean {
    const t = this.peek();
    return (
      t.kind === 'number' ||
      t.kind === 'constant' ||
      t.kind === 'ident' ||
      t.kind === 'lparen'
      // A leading '-' is NOT a value start, so `2 - 3` stays subtraction
      // rather than implicit mul `2 * (-3)`.
    );
  }
}
