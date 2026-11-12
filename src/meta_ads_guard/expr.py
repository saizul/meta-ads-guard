"""A small, safe expression language for rule conditions.

Conditions are never passed to :func:`eval`. They are tokenised and parsed
into a tree by a recursive-descent parser, then evaluated against a resolver
that supplies metric values.

Examples::

    spend > 1500 BDT and purchases == 0
    frequency > 3.5
    cpa > target_cpa × 1.4 and results >= 3
    spend[today] > 2 * spend[yesterday]
    not (ctr >= 0.8) or cpm > 450
    0.5 < roas < 1.2

Grammar (lowest to highest precedence)::

    or       :=  and ( ("or" | "||") and )*
    and      :=  not ( ("and" | "&&") not )*
    not      :=  ("not" | "!") not  |  compare
    compare  :=  sum ( (">" | ">=" | "<" | "<=" | "==" | "!=" | "=") sum )*
    sum      :=  term ( ("+" | "-") term )*
    term     :=  unary ( ("*" | "×" | "x" | "/" | "÷") unary )*
    unary    :=  "-" unary  |  primary
    primary  :=  NUMBER [CURRENCY] ["%"]
              |  NAME [ "[" WINDOW "]" ]
              |  NAME "(" [ or ( "," or )* ] ")"
              |  "(" or ")"
              |  true | false | null

Missing values (``null``, a metric that cannot be computed such as CPA with no
results, or a division by zero) propagate through arithmetic and make every
comparison false, so a rule never fires on data it does not have.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

from .errors import ExpressionError

Value = Union[float, bool, None]

# -- tokens -----------------------------------------------------------------

_TOKEN_SPEC = [
    ("WS", r"\s+"),
    ("NUMBER", r"\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+"),
    ("WINDOW", r"\[[^\]]*\]"),
    ("OP", r">=|<=|==|!=|&&|\|\||[<>=!+\-*/×÷(),%]"),
    ("NAME", r"[A-Za-z_][A-Za-z0-9_.:]*"),
]
_TOKEN_RE = re.compile("|".join(f"(?P<{n}>{p})" for n, p in _TOKEN_SPEC))
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_KEYWORDS = {"and", "or", "not", "true", "false", "null", "none", "x"}


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    pos: int


def tokenize(source: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    while pos < len(source):
        m = _TOKEN_RE.match(source, pos)
        if not m:
            raise ExpressionError(f"unexpected character {source[pos]!r}", source, pos)
        kind = m.lastgroup or ""
        if kind != "WS":
            text = m.group()
            if kind == "NAME" and text.lower() in _KEYWORDS:
                kind = "KW"
                text = text.lower()
            tokens.append(Token(kind, text, pos))
        pos = m.end()
    tokens.append(Token("END", "", len(source)))
    return tokens


# -- tree -------------------------------------------------------------------


class Node:
    pos: int = 0

    def evaluate(self, resolver: Resolver) -> Value:
        raise NotImplementedError

    def children(self) -> list[Node]:
        return []

    def walk(self):
        yield self
        for child in self.children():
            yield from child.walk()


Resolver = Callable[[str, Optional[str]], Value]


@dataclass
class Num(Node):
    value: float
    currency: Optional[str] = None
    pos: int = 0

    def evaluate(self, resolver: Resolver) -> Value:
        return self.value


@dataclass
class Const(Node):
    value: Value
    pos: int = 0

    def evaluate(self, resolver: Resolver) -> Value:
        return self.value


@dataclass
class Var(Node):
    name: str
    window: Optional[str] = None
    pos: int = 0

    def evaluate(self, resolver: Resolver) -> Value:
        return resolver(self.name, self.window)


@dataclass
class Neg(Node):
    operand: Node
    pos: int = 0

    def evaluate(self, resolver: Resolver) -> Value:
        v = _number(self.operand.evaluate(resolver))
        return None if v is None else -v

    def children(self) -> list[Node]:
        return [self.operand]


@dataclass
class BinOp(Node):
    op: str
    left: Node
    right: Node
    pos: int = 0

    def evaluate(self, resolver: Resolver) -> Value:
        a = _number(self.left.evaluate(resolver))
        b = _number(self.right.evaluate(resolver))
        if a is None or b is None:
            return None
        if self.op == "+":
            return a + b
        if self.op == "-":
            return a - b
        if self.op == "*":
            return a * b
        if b == 0:
            return None
        return a / b

    def children(self) -> list[Node]:
        return [self.left, self.right]


_EPS = 1e-9


def _compare(op: str, a: float, b: float) -> bool:
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b or math.isclose(a, b, abs_tol=_EPS)
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b or math.isclose(a, b, abs_tol=_EPS)
    if op == "==":
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=_EPS)
    if op == "!=":
        return not math.isclose(a, b, rel_tol=1e-9, abs_tol=_EPS)
    raise AssertionError(op)


@dataclass
class Compare(Node):
    ops: list[str]
    operands: list[Node]
    pos: int = 0

    def evaluate(self, resolver: Resolver) -> Value:
        values = [_number(n.evaluate(resolver)) for n in self.operands]
        for i, op in enumerate(self.ops):
            a, b = values[i], values[i + 1]
            if a is None or b is None:
                return False
            if not _compare(op, a, b):
                return False
        return True

    def children(self) -> list[Node]:
        return list(self.operands)


@dataclass
class Logic(Node):
    op: str  # "and" | "or"
    items: list[Node]
    pos: int = 0

    def evaluate(self, resolver: Resolver) -> Value:
        if self.op == "and":
            return all(_truthy(n.evaluate(resolver)) for n in self.items)
        return any(_truthy(n.evaluate(resolver)) for n in self.items)

    def children(self) -> list[Node]:
        return list(self.items)


@dataclass
class Not(Node):
    operand: Node
    pos: int = 0

    def evaluate(self, resolver: Resolver) -> Value:
        return not _truthy(self.operand.evaluate(resolver))

    def children(self) -> list[Node]:
        return [self.operand]


def _fn_min(*args: Value) -> Value:
    vals = [v for v in (_number(a) for a in args) if v is not None]
    return min(vals) if vals else None


def _fn_max(*args: Value) -> Value:
    vals = [v for v in (_number(a) for a in args) if v is not None]
    return max(vals) if vals else None


def _fn_abs(x: Value) -> Value:
    v = _number(x)
    return None if v is None else abs(v)


def _fn_coalesce(*args: Value) -> Value:
    for a in args:
        if a is not None:
            return a
    return None


def _fn_round(x: Value, digits: Value = 0) -> Value:
    v = _number(x)
    d = _number(digits)
    return None if v is None else round(v, int(d or 0))


def _fn_missing(x: Value) -> Value:
    return x is None


FUNCTIONS: dict[str, tuple[Callable[..., Value], int, Optional[int]]] = {
    # name: (function, min args, max args or None)
    "min": (_fn_min, 1, None),
    "max": (_fn_max, 1, None),
    "abs": (_fn_abs, 1, 1),
    "coalesce": (_fn_coalesce, 1, None),
    "round": (_fn_round, 1, 2),
    "missing": (_fn_missing, 1, 1),
}


@dataclass
class Call(Node):
    name: str
    args: list[Node]
    pos: int = 0

    def evaluate(self, resolver: Resolver) -> Value:
        fn = FUNCTIONS[self.name][0]
        return fn(*(a.evaluate(resolver) for a in self.args))

    def children(self) -> list[Node]:
        return list(self.args)


def _number(value: Value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    return None


def _truthy(value: Value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return bool(value)


# -- parser -----------------------------------------------------------------


class _Parser:
    def __init__(self, source: str):
        self.source = source
        self.tokens = tokenize(source)
        self.i = 0

    @property
    def tok(self) -> Token:
        return self.tokens[self.i]

    def error(self, message: str, tok: Optional[Token] = None) -> ExpressionError:
        tok = tok or self.tok
        return ExpressionError(message, self.source, tok.pos)

    def accept(self, *texts: str) -> Optional[Token]:
        if self.tok.kind in {"OP", "KW"} and self.tok.text in texts:
            tok = self.tok
            self.i += 1
            return tok
        return None

    def expect(self, text: str) -> Token:
        tok = self.accept(text)
        if tok is None:
            found = self.tok.text or "end of expression"
            raise self.error(f"expected {text!r}, found {found!r}")
        return tok

    def parse(self) -> Node:
        if self.tok.kind == "END":
            raise self.error("empty expression")
        node = self.parse_or()
        if self.tok.kind != "END":
            raise self.error(f"unexpected {self.tok.text!r}")
        return node

    def parse_or(self) -> Node:
        first = self.tok
        items = [self.parse_and()]
        while self.accept("or", "||"):
            items.append(self.parse_and())
        return items[0] if len(items) == 1 else Logic("or", items, first.pos)

    def parse_and(self) -> Node:
        first = self.tok
        items = [self.parse_not()]
        while self.accept("and", "&&"):
            items.append(self.parse_not())
        return items[0] if len(items) == 1 else Logic("and", items, first.pos)

    def parse_not(self) -> Node:
        tok = self.accept("not", "!")
        if tok:
            return Not(self.parse_not(), tok.pos)
        return self.parse_compare()

    def parse_compare(self) -> Node:
        first = self.tok
        operands = [self.parse_sum()]
        ops: list[str] = []
        while True:
            tok = self.accept(">", ">=", "<", "<=", "==", "!=", "=")
            if not tok:
                break
            ops.append("==" if tok.text == "=" else tok.text)
            operands.append(self.parse_sum())
        if not ops:
            return operands[0]
        return Compare(ops, operands, first.pos)

    def parse_sum(self) -> Node:
        node = self.parse_term()
        while True:
            tok = self.accept("+", "-")
            if not tok:
                return node
            node = BinOp(tok.text, node, self.parse_term(), tok.pos)

    def parse_term(self) -> Node:
        node = self.parse_unary()
        while True:
            tok = self.accept("*", "×", "x", "/", "÷")
            if not tok:
                return node
            op = "/" if tok.text in {"/", "÷"} else "*"
            node = BinOp(op, node, self.parse_unary(), tok.pos)

    def parse_unary(self) -> Node:
        tok = self.accept("-")
        if tok:
            return Neg(self.parse_unary(), tok.pos)
        self.accept("+")
        return self.parse_primary()

    def parse_primary(self) -> Node:
        tok = self.tok
        if tok.kind == "NUMBER":
            self.i += 1
            value = float(tok.text.replace("_", ""))
            currency = None
            if self.tok.kind == "NAME" and _CURRENCY_RE.match(self.tok.text):
                currency = self.tok.text
                self.i += 1
            self.accept("%")
            return Num(value, currency, tok.pos)
        if tok.kind == "KW" and tok.text in {"true", "false", "null", "none"}:
            self.i += 1
            value: Value = {"true": True, "false": False}.get(tok.text)
            return Const(value, tok.pos)
        if tok.kind == "NAME":
            self.i += 1
            if self.accept("("):
                return self.parse_call(tok)
            window = None
            if self.tok.kind == "WINDOW":
                window = self.tok.text[1:-1].strip()
                if not window:
                    raise self.error("empty window in brackets")
                self.i += 1
            return Var(tok.text, window, tok.pos)
        if self.accept("("):
            node = self.parse_or()
            self.expect(")")
            return node
        if tok.kind == "END":
            raise self.error("unexpected end of expression")
        raise self.error(f"unexpected {tok.text!r}")

    def parse_call(self, name_tok: Token) -> Node:
        name = name_tok.text.lower()
        if name not in FUNCTIONS:
            known = ", ".join(sorted(FUNCTIONS))
            raise self.error(f"unknown function {name_tok.text!r} (known: {known})", name_tok)
        args: list[Node] = []
        if not self.accept(")"):
            args.append(self.parse_or())
            while self.accept(","):
                args.append(self.parse_or())
            self.expect(")")
        _, lo, hi = FUNCTIONS[name]
        if len(args) < lo or (hi is not None and len(args) > hi):
            raise self.error(f"{name}() takes {lo}..{hi or 'n'} arguments", name_tok)
        return Call(name, args, name_tok.pos)


@dataclass
class Expression:
    """A parsed condition."""

    source: str
    root: Node

    def evaluate(self, resolver: Resolver) -> bool:
        return _truthy(self.root.evaluate(resolver))

    def value(self, resolver: Resolver) -> Value:
        return self.root.evaluate(resolver)

    def variables(self) -> list[Var]:
        return [n for n in self.root.walk() if isinstance(n, Var)]

    def currencies(self) -> set[str]:
        return {n.currency for n in self.root.walk() if isinstance(n, Num) and n.currency}

    def windows(self) -> set[str]:
        return {v.window for v in self.variables() if v.window}

    def __str__(self) -> str:
        return self.source


def parse(source: Any) -> Expression:
    if not isinstance(source, str):
        raise ExpressionError(f"condition must be a string, got {type(source).__name__}")
    return Expression(source, _Parser(source).parse())


def explain(expression: Expression, resolver: Resolver) -> list[tuple[str, Value]]:
    """Return ``(reference, value)`` pairs for every variable, for reports."""
    seen: dict[str, Value] = {}
    for var in expression.variables():
        ref = f"{var.name}[{var.window}]" if var.window else var.name
        if ref not in seen:
            seen[ref] = resolver(var.name, var.window)
    return list(seen.items())
