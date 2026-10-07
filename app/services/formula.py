"""
A tiny, safe arithmetic language for the simulator activities.

    formula := expr
    expr    := term (("+" | "-") term)*
    term    := factor (("*" | "/") factor)*
    factor  := ("+" | "-") factor | number | variable | ("min" | "max") "(" expr "," expr ")" | "(" expr ")"

Only the variables an activity declares can be named. The text is parsed into a small tree that is walked, never
handed to `eval`: an AI-written formula can compute, nothing else. `src/lib/formula.ts` in the frontend is the same
grammar, so the learner sees the live result the server will confirm.
"""

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import product

MAX_FORMULA_CHARS = 200
MAX_DEPTH = 32  # nesting of parentheses and signs

_TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d+)?|\.\d+)|([A-Za-z_][A-Za-z0-9_]*)|(.))")
FUNCTIONS = ("min", "max")


class FormulaError(ValueError):
    """The formula can't be used: the message says why, in Spanish, for the admin."""


@dataclass(frozen=True)
class Num:
    value: float


@dataclass(frozen=True)
class Var:
    name: str


@dataclass(frozen=True)
class Neg:
    operand: "Node"


@dataclass(frozen=True)
class BinOp:
    op: str  # + - * /
    left: "Node"
    right: "Node"


@dataclass(frozen=True)
class Call:
    name: str  # min | max
    left: "Node"
    right: "Node"


Node = Num | Var | Neg | BinOp | Call


def _tokens(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    text = text.rstrip()
    while position < len(text):
        match = _TOKEN.match(text, position)
        if not match:  # only trailing spaces are left
            break
        number, name, symbol = match.groups()
        if number is not None:
            tokens.append(("num", number))
        elif name is not None:
            tokens.append(("name", name))
        elif symbol in "+-*/(),":
            tokens.append(("op", symbol))
        else:
            raise FormulaError(f"La fórmula tiene un símbolo no permitido: «{symbol}».")
        position = match.end()
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]], variables: Iterable[str]):
        self.tokens = tokens
        self.position = 0
        self.variables = set(variables)

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self, value: str | None = None) -> tuple[str, str]:
        token = self.peek()
        if token is None or (value is not None and token[1] != value):
            raise FormulaError(f"A la fórmula le falta «{value}»." if value else "La fórmula está incompleta.")
        self.position += 1
        return token

    def expr(self, depth: int) -> Node:
        node = self.term(depth)
        while (token := self.peek()) and token[1] in ("+", "-"):
            self.take()
            node = BinOp(token[1], node, self.term(depth))
        return node

    def term(self, depth: int) -> Node:
        node = self.factor(depth)
        while (token := self.peek()) and token[1] in ("*", "/"):
            self.take()
            node = BinOp(token[1], node, self.factor(depth))
        return node

    def factor(self, depth: int) -> Node:
        if depth > MAX_DEPTH:
            raise FormulaError("La fórmula está demasiado anidada.")
        token = self.take()
        kind, value = token
        if kind == "op" and value in ("+", "-"):
            operand = self.factor(depth + 1)
            return Neg(operand) if value == "-" else operand
        if kind == "num":
            return Num(float(value))
        if kind == "name" and value in FUNCTIONS:
            self.take("(")
            left = self.expr(depth + 1)
            self.take(",")
            right = self.expr(depth + 1)
            self.take(")")
            return Call(value, left, right)
        if kind == "name":
            if value not in self.variables:
                raise FormulaError(f"La fórmula usa «{value}», que no es una de las variables del simulador.")
            return Var(value)
        if value == "(":
            node = self.expr(depth + 1)
            self.take(")")
            return node
        raise FormulaError(f"La fórmula no se entiende cerca de «{value}».")


def parse(text: str, variables: Iterable[str]) -> Node:
    """The formula's tree, or FormulaError if it isn't valid with these variables."""
    if not text.strip():
        raise FormulaError("Escribe la fórmula del simulador.")
    if len(text) > MAX_FORMULA_CHARS:
        raise FormulaError(f"La fórmula es demasiado larga (máximo {MAX_FORMULA_CHARS} caracteres).")
    parser = _Parser(_tokens(text), variables)
    node = parser.expr(0)
    if parser.peek() is not None:
        raise FormulaError(f"La fórmula no se entiende cerca de «{parser.peek()[1]}».")
    return node


def evaluate(node: Node, values: dict[str, float]) -> float:
    """The formula's value for these variables; FormulaError on a division by zero or a non-finite result."""
    match node:
        case Num(value):
            result = value
        case Var(name):
            result = float(values[name])
        case Neg(operand):
            result = -evaluate(operand, values)
        case Call(name, left, right):
            result = (min if name == "min" else max)(evaluate(left, values), evaluate(right, values))
        case BinOp(op, left, right):
            a, b = evaluate(left, values), evaluate(right, values)
            if op == "/" and b == 0:
                raise FormulaError("La fórmula divide entre cero con algunos valores de los controles.")
            result = a + b if op == "+" else a - b if op == "-" else a * b if op == "*" else a / b
    if not math.isfinite(result):
        raise FormulaError("La fórmula da un resultado que no es un número con algunos valores de los controles.")
    return result


def corners(ranges: dict[str, tuple[float, float]]) -> list[dict[str, float]]:
    """Every combination of each variable at its minimum and maximum (at most 2^4 = 16 for a simulator)."""
    names = list(ranges)
    return [dict(zip(names, combo)) for combo in product(*(ranges[name] for name in names))]
