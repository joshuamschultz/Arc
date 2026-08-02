"""COMP-003 — the frozen v1 predicate grammar (SPEC-061 REQ-219).

``when`` conditions and router routes are the only place a workflow makes a
decision from data, so this is a security boundary, not a convenience parser.
It is hand-written recursive descent over a whitelisted AST of exactly four
node types — path, literal, comparison, boolean — and there is deliberately no
production for a call, an attribute walk, an import, an assignment, a
comprehension, or arithmetic. An unsupported construct therefore fails at
*parse* time and can never reach evaluation (ASI05/LLM05).

Two further rules keep it honest:

* **Only ``$nodes`` and ``$input`` exist as roots.** There is no clock, no
  randomness, and no environment read, so wiring is deterministic and a signed
  definition means the same thing on every host.
* **Paths resolve by mapping lookup, never ``getattr``.** A scope value's
  Python attributes are invisible to the grammar, which is what makes
  traversal into an object graph impossible rather than merely discouraged.

The grammar is frozen workflow-wide at v1 — no per-node dialect switching. If
a richer language is ever needed it swaps in behind :func:`evaluate`, which is
the single seam every caller uses.

    expression  := or_expr
    or_expr     := and_expr ( "or" and_expr )*
    and_expr    := unary ( "and" unary )*
    unary       := "not" unary | primary
    primary     := "(" expression ")" | comparison
    comparison  := operand ( op operand )?     op in == != < <= > >= in, not in
    operand     := path | literal | list_literal
    path        := "$" ("nodes"|"input") ("." segment)+
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from arcteam.workflow.errors import PredicateEvaluationError, PredicateParseError

MAX_EXPRESSION_LENGTH = 1_000
"""Refused before tokenizing — an unbounded predicate is a parser DoS (LLM10)."""

MAX_DEPTH = 32
"""Recursion ceiling for nested parentheses and boolean nesting."""

_COMPARISONS = ("==", "!=", "<=", ">=", "<", ">")
_SEGMENT = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


# --- the whitelisted AST -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class PathRef:
    """A ``$nodes.<id>.output.<field>...`` or ``$input.<field>...`` reference."""

    root: Literal["nodes", "input"]
    segments: tuple[str, ...]

    @property
    def node_id(self) -> str | None:
        """The node this path reads, or ``None`` for a run-input path."""
        return self.segments[0] if self.root == "nodes" and self.segments else None

    def __str__(self) -> str:
        return "$" + ".".join((self.root, *self.segments))


@dataclass(frozen=True, slots=True)
class LiteralValue:
    """A string, number, boolean, null, or list-of-literals constant."""

    value: Any


@dataclass(frozen=True, slots=True)
class Compare:
    """One comparison between two operands. Chaining is not in the grammar."""

    left: PathRef | LiteralValue
    operator: str
    right: PathRef | LiteralValue


@dataclass(frozen=True, slots=True)
class Not:
    """Boolean negation."""

    operand: Predicate


@dataclass(frozen=True, slots=True)
class BoolOp:
    """``and``/``or`` over two or more operands."""

    op: Literal["and", "or"]
    operands: tuple[Predicate, ...]


Predicate = PathRef | LiteralValue | Compare | Not | BoolOp


# --- tokenizer ---------------------------------------------------------------

_TOKEN_PATTERN = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<path>\$[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)*)
  | (?P<string>'[^'\\]*'|"[^"\\]*")
  | (?P<number>-?\d+\.\d+|-?\d+)
  | (?P<op>==|!=|<=|>=|<|>)
  | (?P<punct>[()\[\],])
  | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)

_KEYWORDS = frozenset({"and", "or", "not", "in", "true", "false", "null"})


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    text: str
    position: int


def _tokenize(expression: str) -> list[_Token]:
    """Split into tokens, refusing anything outside the grammar's alphabet."""
    tokens: list[_Token] = []
    index = 0
    while index < len(expression):
        match = _TOKEN_PATTERN.match(expression, index)
        if match is None:
            raise PredicateParseError(
                f"unsupported character {expression[index]!r} at position {index} "
                f"in predicate {expression!r}"
            )
        index = match.end()
        kind = match.lastgroup or ""
        if kind == "space":
            continue
        text = match.group()
        if kind == "word" and text.lower() not in _KEYWORDS:
            raise PredicateParseError(
                f"bare identifier {text!r} is not part of the predicate grammar — "
                f"there are no functions, variables, or environment reads; "
                f"use $nodes.<id>.output.<field> or $input.<field>"
            )
        tokens.append(_Token(kind, text, match.start()))
    return tokens


# --- parser ------------------------------------------------------------------


class _Parser:
    """Recursive-descent parser over the token stream."""

    def __init__(self, tokens: list[_Token], expression: str) -> None:
        self._tokens = tokens
        self._expression = expression
        self._index = 0

    def parse(self) -> Predicate:
        predicate = self._parse_or(depth=0)
        if self._index != len(self._tokens):
            token = self._tokens[self._index]
            raise PredicateParseError(
                f"unexpected {token.text!r} at position {token.position} "
                f"in predicate {self._expression!r}"
            )
        return predicate

    # -- productions --

    def _parse_or(self, depth: int) -> Predicate:
        operands = [self._parse_and(self._deeper(depth))]
        while self._take_word("or"):
            operands.append(self._parse_and(self._deeper(depth)))
        return operands[0] if len(operands) == 1 else BoolOp("or", tuple(operands))

    def _parse_and(self, depth: int) -> Predicate:
        operands = [self._parse_unary(self._deeper(depth))]
        while self._take_word("and"):
            operands.append(self._parse_unary(self._deeper(depth)))
        return operands[0] if len(operands) == 1 else BoolOp("and", tuple(operands))

    def _parse_unary(self, depth: int) -> Predicate:
        if self._take_word("not"):
            return Not(self._parse_unary(self._deeper(depth)))
        return self._parse_primary(depth)

    def _parse_primary(self, depth: int) -> Predicate:
        if self._take_punct("("):
            inner = self._parse_or(self._deeper(depth))
            self._expect_punct(")")
            return inner
        return self._parse_comparison()

    def _parse_comparison(self) -> Predicate:
        left = self._parse_operand()
        operator = self._take_comparison_operator()
        if operator is None:
            return left
        return Compare(left, operator, self._parse_operand())

    def _parse_operand(self) -> PathRef | LiteralValue:
        token = self._peek()
        if token is None:
            raise PredicateParseError(f"predicate {self._expression!r} ends where a value is due")
        if token.kind == "path":
            self._index += 1
            return _parse_path(token.text)
        if token.kind == "string":
            self._index += 1
            return LiteralValue(token.text[1:-1])
        if token.kind == "number":
            self._index += 1
            return LiteralValue(float(token.text) if "." in token.text else int(token.text))
        if token.kind == "word":
            self._index += 1
            return LiteralValue({"true": True, "false": False, "null": None}[token.text.lower()])
        if token.kind == "punct" and token.text == "[":
            return self._parse_list_literal()
        raise PredicateParseError(
            f"unexpected {token.text!r} at position {token.position} where a value is due "
            f"in predicate {self._expression!r}"
        )

    def _parse_list_literal(self) -> LiteralValue:
        self._expect_punct("[")
        items: list[Any] = []
        if not self._take_punct("]"):
            while True:
                operand = self._parse_operand()
                if not isinstance(operand, LiteralValue):
                    raise PredicateParseError("a list literal may hold constants only")
                items.append(operand.value)
                if self._take_punct("]"):
                    break
                self._expect_punct(",")
        return LiteralValue(items)

    # -- token helpers --

    def _peek(self) -> _Token | None:
        return self._tokens[self._index] if self._index < len(self._tokens) else None

    def _take_word(self, word: str) -> bool:
        token = self._peek()
        if token is not None and token.kind == "word" and token.text.lower() == word:
            self._index += 1
            return True
        return False

    def _take_punct(self, punct: str) -> bool:
        token = self._peek()
        if token is not None and token.kind == "punct" and token.text == punct:
            self._index += 1
            return True
        return False

    def _expect_punct(self, punct: str) -> None:
        if not self._take_punct(punct):
            token = self._peek()
            found = token.text if token else "end of expression"
            raise PredicateParseError(
                f"expected {punct!r} but found {found!r} in predicate {self._expression!r}"
            )

    def _take_comparison_operator(self) -> str | None:
        token = self._peek()
        if token is None:
            return None
        if token.kind == "op":
            self._index += 1
            return token.text
        if token.kind == "word" and token.text.lower() == "in":
            self._index += 1
            return "in"
        if token.kind == "word" and token.text.lower() == "not":
            saved = self._index
            self._index += 1
            if self._take_word("in"):
                return "not in"
            self._index = saved
        return None

    def _deeper(self, depth: int) -> int:
        if depth >= MAX_DEPTH:
            raise PredicateParseError(
                f"predicate nests deeper than {MAX_DEPTH} levels; simplify the condition"
            )
        return depth + 1


def _parse_path(text: str) -> PathRef:
    """Turn ``$nodes.verify.output.risk`` into a :class:`PathRef`."""
    parts = text[1:].split(".")
    root: Literal["nodes", "input"]
    if parts[0] == "nodes":
        root = "nodes"
    elif parts[0] == "input":
        root = "input"
    else:
        raise PredicateParseError(
            f"unknown reference root ${parts[0]} — the only roots are $nodes and $input "
            f"(there is no clock, randomness, or environment in a predicate)"
        )
    segments = tuple(parts[1:])
    if not segments:
        raise PredicateParseError(f"{text!r} names a root with no field")
    for segment in segments:
        if not _SEGMENT.match(segment):
            raise PredicateParseError(
                f"invalid path segment {segment!r} in {text!r} — segments must start with a "
                f"letter and hold only letters, digits, '_' and '-'"
            )
    if root == "nodes" and (len(segments) < 3 or segments[1] != "output"):
        raise PredicateParseError(
            f"{text!r} must read a node output: $nodes.<node_id>.output.<field>"
        )
    return PathRef(root, segments)


def parse_predicate(expression: str) -> Predicate:
    """Parse ``expression`` into the whitelisted AST.

    Raises:
        PredicateParseError: for anything outside the frozen v1 grammar.
    """
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise PredicateParseError(
            f"predicate is {len(expression)} characters; the ceiling is {MAX_EXPRESSION_LENGTH}"
        )
    tokens = _tokenize(expression)
    if not tokens:
        raise PredicateParseError("empty predicate")
    return _Parser(tokens, expression).parse()


# --- evaluation --------------------------------------------------------------


def evaluate(expression: str, scope: Mapping[str, Any]) -> bool:
    """Evaluate ``expression`` against ``scope``. The one seam every caller uses.

    Args:
        expression: A predicate in the frozen v1 grammar.
        scope: ``{"nodes": {node_id: {"output": {...}}}, "input": {...}}``.

    Returns:
        The predicate's truth value.

    Raises:
        PredicateParseError: the expression is outside the grammar.
        PredicateEvaluationError: a referenced path is absent, or a comparison
            is meaningless for the values found. Both fail closed rather than
            collapsing to ``False``, because a silently-false router condition
            takes a branch nobody chose.
    """
    return bool(_evaluate_node(parse_predicate(expression), scope))


def referenced_nodes(predicate: Predicate) -> frozenset[str]:
    """Every node id whose output ``predicate`` reads."""
    return frozenset(path.node_id for path in paths_in(predicate) if path.node_id is not None)


def paths_in(predicate: Predicate) -> tuple[PathRef, ...]:
    """Every :class:`PathRef` in the tree, in no particular order."""
    if isinstance(predicate, PathRef):
        return (predicate,)
    if isinstance(predicate, LiteralValue):
        return ()
    if isinstance(predicate, Not):
        return paths_in(predicate.operand)
    if isinstance(predicate, BoolOp):
        return tuple(path for operand in predicate.operands for path in paths_in(operand))
    return paths_in(predicate.left) + paths_in(predicate.right)


def _evaluate_node(predicate: Predicate, scope: Mapping[str, Any]) -> Any:
    if isinstance(predicate, LiteralValue):
        return predicate.value
    if isinstance(predicate, PathRef):
        return _resolve_path(predicate, scope)
    if isinstance(predicate, Not):
        return not _evaluate_node(predicate.operand, scope)
    if isinstance(predicate, BoolOp):
        results = (bool(_evaluate_node(o, scope)) for o in predicate.operands)
        return all(results) if predicate.op == "and" else any(results)
    return _evaluate_comparison(predicate, scope)


def _evaluate_comparison(predicate: Compare, scope: Mapping[str, Any]) -> bool:
    return _compare(
        _evaluate_node(predicate.left, scope),
        predicate.operator,
        _evaluate_node(predicate.right, scope),
    )


def _compare(left: Any, operator: str, right: Any) -> bool:
    """Apply one operator, turning a meaningless comparison into a typed error."""
    if operator == "==":
        return bool(left == right)
    if operator == "!=":
        return bool(left != right)
    try:
        if operator == "in":
            return left in right
        if operator == "not in":
            return left not in right
        if operator == "<":
            return bool(left < right)
        if operator == "<=":
            return bool(left <= right)
        if operator == ">":
            return bool(left > right)
        return bool(left >= right)
    except TypeError as exc:
        raise PredicateEvaluationError(
            f"cannot evaluate {left!r} {operator} {right!r}: {exc}"
        ) from exc


def _resolve_path(path: PathRef, scope: Mapping[str, Any]) -> Any:
    """Walk ``path`` through ``scope`` by mapping lookup only.

    ``getattr`` is never used, so a scope value's Python attributes — and any
    object graph reachable through them — are invisible to the grammar.
    """
    current: Any = scope.get(path.root)
    walked: list[str] = [path.root]
    for segment in path.segments:
        if not isinstance(current, Mapping) or segment not in current:
            raise PredicateEvaluationError(
                f"{path} is unresolvable: no {segment!r} under ${'.'.join(walked)}"
            )
        current = current[segment]
        walked.append(segment)
    return current


__all__ = [
    "MAX_DEPTH",
    "MAX_EXPRESSION_LENGTH",
    "BoolOp",
    "Compare",
    "LiteralValue",
    "Not",
    "PathRef",
    "Predicate",
    "evaluate",
    "parse_predicate",
    "paths_in",
    "referenced_nodes",
]
