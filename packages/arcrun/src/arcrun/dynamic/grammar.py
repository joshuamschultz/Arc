"""The frozen v1 script grammar — a whitelisted subset of Python.

A dynamic script is written by the model, so it is untrusted input (LLM01) that
we then execute (ASI05). The defence is that the language it is written in has
no production for escaping: no import, no attribute walk, no ``eval``, no call
to anything the host did not put in the namespace, and no clock, randomness or
environment read. An unsupported construct fails at *parse* time and can
therefore never reach evaluation.

This mirrors ``arcteam.workflow.predicates`` one tier up in expressiveness: that
grammar decides a branch, this one orchestrates a run. Both are whitelists, both
fail closed, and neither uses ``getattr``.

Two rules carry most of the weight:

* **Names must already exist.** Every read is checked against the host
  namespace and what the script itself has bound, in order, so a typo or a
  reach for ``os`` is a parse error rather than a runtime surprise.
* **Method calls dispatch through a table, never ``getattr``.** ``jobs.append(x)``
  is allowed because ``append`` is one of a fixed set of names the interpreter
  implements itself against built-in container types. A receiver's real Python
  attributes stay invisible, so allowing methods opens no path into an object
  graph.

Determinism is a security property here and not just a nicety: the journal
resumes a run by re-executing the same script and replaying recorded host
results, which only holds if the script cannot observe time, randomness, or
anything else that moves between runs.
"""

from __future__ import annotations

import ast
from typing import Final

MAX_SCRIPT_BYTES: Final = 64 * 1024
"""Refused before parsing — an unbounded script is a parser DoS (LLM10)."""

MAX_NESTING: Final = 12
"""Control-flow nesting ceiling, so the tree walk cannot exhaust the stack."""

MAX_EXPRESSION_DEPTH: Final = 32
"""Expression nesting ceiling.

Statement nesting alone leaves the stack open: ``1+1+...+1`` is one statement
whose tree is thousands deep, and both this validator and the interpreter walk
it recursively. Bounding depth here is what keeps a ``RecursionError`` from
escaping as an untyped crash.
"""

HOST_FUNCTIONS: Final = frozenset(
    {
        "agent",
        "parallel",
        "phase",
        "log",
        "budget",
        "scratch_read",
        "scratch_write",
        "complete",
        "pause",
    }
)
"""Every effect available to a script. Mirrors ``host.ScriptHost``."""

ALLOWED_BUILTINS: Final = frozenset(
    {
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "float",
        "int",
        "json_encode",
        "len",
        "list",
        "max",
        "min",
        "range",
        "reversed",
        "round",
        "sorted",
        "str",
        "sum",
        "zip",
    }
)
"""Pure helpers. Nothing here reads the clock, the environment, or the disk."""

PREDEFINED_NAMES: Final = frozenset({"args"})
"""Values the host binds before the first statement runs."""

ALLOWED_METHODS: Final = frozenset(
    {
        "append",
        "count",
        "endswith",
        "extend",
        "get",
        "index",
        "items",
        "join",
        "keys",
        "lower",
        "replace",
        "sort",
        "split",
        "startswith",
        "strip",
        "upper",
        "values",
    }
)
"""Method names the interpreter implements itself for built-in containers."""

CALLABLE_NAMES: Final = HOST_FUNCTIONS | ALLOWED_BUILTINS
"""The only bare names that may appear as a call target.

A name the script itself bound holds a *value*, and a value is not callable:
without this, ``x = "notes"`` followed by ``x("notes")`` was legal syntax and
the interpreter dispatched it to whichever host function it fell through to.
Binding one of these names is refused for the same reason — shadowing
``agent`` would let a script make a call read as something it is not.
"""

_ALLOWED_STATEMENTS: Final = (
    ast.Expr,
    ast.Assign,
    ast.AugAssign,
    ast.For,
    ast.While,
    ast.If,
    ast.Break,
    ast.Continue,
    ast.Pass,
)

_ALLOWED_EXPRESSIONS: Final = (
    ast.Name,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Subscript,
    ast.Slice,
    ast.Compare,
    ast.BoolOp,
    ast.UnaryOp,
    ast.BinOp,
    ast.IfExp,
    ast.ListComp,
    ast.Call,
    ast.JoinedStr,
    ast.FormattedValue,
)

_ALLOWED_BINOPS: Final = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod)
"""``Pow`` is absent on purpose: ``10**10**10`` is a one-token memory bomb."""

_ALLOWED_COMPARES: Final = (
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
)
"""``Is``/``IsNot`` are absent: object identity has no meaning to a script."""

_ALLOWED_UNARYOPS: Final = (ast.Not, ast.USub, ast.UAdd)

_ALLOWED_CONSTANTS: Final = (str, int, float, bool, type(None))


class ScriptSyntaxError(Exception):
    """A script is outside the frozen v1 grammar. Raised before any execution."""


def parse_script(source: str) -> ast.Module:
    """Parse and validate ``source``, returning the tree the interpreter walks.

    Args:
        source: The script as written by the model.

    Returns:
        The validated module. Every node in it is one the interpreter handles.

    Raises:
        ScriptSyntaxError: For anything outside the grammar, including a plain
            Python syntax error, an unknown name, or excessive size or nesting.
    """
    encoded = source.encode("utf-8")
    if len(encoded) > MAX_SCRIPT_BYTES:
        raise ScriptSyntaxError(
            f"script is {len(encoded)} bytes; the ceiling is {MAX_SCRIPT_BYTES} bytes"
        )
    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        line = exc.lineno if exc.lineno is not None else 0
        raise ScriptSyntaxError(f"line {line}: {exc.msg}") from exc
    _Validator().check_module(module)
    return module


def _fail(node: ast.AST, message: str) -> ScriptSyntaxError:
    """Build an error that names the line, because the model has to fix it."""
    line = getattr(node, "lineno", 0)
    return ScriptSyntaxError(f"line {line}: {message}")


class _Validator:
    """Walks the tree once, tracking which names are bound at each point.

    Binding is tracked in statement order so a read of a name assigned later is
    a parse error. Branch bodies bind into the enclosing scope after the branch
    is checked, which is deliberately permissive: a name bound only inside an
    ``if`` is readable afterwards, and the interpreter raises if it turns out to
    be genuinely unbound at run time.
    """

    def __init__(self) -> None:
        self._bound: set[str] = set(PREDEFINED_NAMES)

    # --- statements ---------------------------------------------------------

    def check_module(self, module: ast.Module) -> None:
        """Validate every statement in the module body."""
        self._check_body(module.body, depth=0)

    def _check_body(self, body: list[ast.stmt], *, depth: int) -> None:
        if depth > MAX_NESTING:
            raise _fail(body[0], f"script nests deeper than {MAX_NESTING} levels")
        for statement in body:
            self._check_statement(statement, depth=depth)

    def _check_statement(self, node: ast.stmt, *, depth: int) -> None:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise _fail(node, "import is not part of the script language")
        if not isinstance(node, _ALLOWED_STATEMENTS):
            raise _fail(node, f"{type(node).__name__} is not part of the script language")

        if isinstance(node, ast.Expr):
            self._check_expression(node.value)
        elif isinstance(node, ast.Assign):
            self._check_expression(node.value)
            for target in node.targets:
                self._bind_target(target)
        elif isinstance(node, ast.AugAssign):
            self._check_expression(node.target)
            self._check_expression(node.value)
            self._bind_target(node.target)
        elif isinstance(node, ast.For):
            self._check_expression(node.iter)
            self._bind_target(node.target)
            self._check_body(node.body, depth=depth + 1)
            if node.orelse:
                self._check_body(node.orelse, depth=depth + 1)
        elif isinstance(node, ast.While):
            self._check_expression(node.test)
            self._check_body(node.body, depth=depth + 1)
            if node.orelse:
                self._check_body(node.orelse, depth=depth + 1)
        elif isinstance(node, ast.If):
            self._check_expression(node.test)
            self._check_body(node.body, depth=depth + 1)
            if node.orelse:
                self._check_body(node.orelse, depth=depth + 1)

    def _bind_target(self, node: ast.expr) -> None:
        """Record the names an assignment or loop target introduces."""
        if isinstance(node, ast.Name):
            if node.id in CALLABLE_NAMES:
                raise _fail(
                    node,
                    f"{node.id!r} is a host function and cannot be reassigned — "
                    f"shadowing it would make a later call read as something it is not",
                )
            self._bound.add(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for element in node.elts:
                self._bind_target(element)
        elif isinstance(node, ast.Subscript):
            self._check_expression(node)
        else:
            raise _fail(node, f"cannot assign to {type(node).__name__}")

    # --- expressions --------------------------------------------------------

    def _check_expression(self, node: ast.expr, depth: int = 0) -> None:
        if depth > MAX_EXPRESSION_DEPTH:
            raise _fail(node, f"expression nests deeper than {MAX_EXPRESSION_DEPTH} levels")
        if isinstance(node, ast.Attribute):
            raise _fail(node, "attribute access is not part of the script language")
        if not isinstance(node, _ALLOWED_EXPRESSIONS):
            raise _fail(node, f"{type(node).__name__} is not part of the script language")

        if isinstance(node, ast.Name):
            self._check_name(node)
        elif isinstance(node, ast.Constant):
            self._check_constant(node)
        elif isinstance(node, ast.Call):
            self._check_call(node, depth)
        elif isinstance(node, ast.Dict):
            self._check_dict(node, depth)
        elif isinstance(node, ast.BinOp):
            self._check_operator(node, node.op, _ALLOWED_BINOPS)
            self._check_expression(node.left, depth + 1)
            self._check_expression(node.right, depth + 1)
        elif isinstance(node, ast.UnaryOp):
            self._check_operator(node, node.op, _ALLOWED_UNARYOPS)
            self._check_expression(node.operand, depth + 1)
        elif isinstance(node, ast.Compare):
            for operator in node.ops:
                self._check_operator(node, operator, _ALLOWED_COMPARES)
            self._check_expression(node.left, depth + 1)
            for comparator in node.comparators:
                self._check_expression(comparator, depth + 1)
        elif isinstance(node, ast.ListComp):
            self._check_comprehension(node, depth)
        else:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.expr):
                    self._check_expression(child, depth + 1)

    def _check_dict(self, node: ast.Dict, depth: int) -> None:
        """A ``None`` key is ``{**other}``, which the generic walk cannot see.

        ``ast.iter_child_nodes`` skips the missing key, so the fallback branch
        would wave the node through and leave the refusal to run time — exactly
        the parse-time guarantee this grammar claims to make.
        """
        for key, value in zip(node.keys, node.values, strict=True):
            if key is None:
                raise _fail(node, "dictionary unpacking is not part of the script language")
            self._check_expression(key, depth + 1)
            self._check_expression(value, depth + 1)

    def _check_name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            return
        if node.id in self._bound or node.id in ALLOWED_BUILTINS or node.id in HOST_FUNCTIONS:
            return
        raise _fail(
            node,
            f"unknown name {node.id!r} — the script language has no imports, "
            f"no environment, and no clock; available names are the host "
            f"functions, a small set of pure builtins, and what this script assigns",
        )

    def _check_constant(self, node: ast.Constant) -> None:
        if not isinstance(node.value, _ALLOWED_CONSTANTS):
            raise _fail(node, f"{type(node.value).__name__} literals are not supported")

    def _check_call(self, node: ast.Call, depth: int = 0) -> None:
        """Check the callee first, so a bad call names the callee not its args."""
        if isinstance(node.func, ast.Attribute):
            if node.func.attr not in ALLOWED_METHODS:
                raise _fail(
                    node,
                    f"unknown method {node.func.attr!r} — only a fixed set of "
                    f"list, dict and string methods exists",
                )
            self._check_expression(node.func.value, depth + 1)
        elif isinstance(node.func, ast.Name):
            self._check_name(node.func)
            if node.func.id not in CALLABLE_NAMES:
                raise _fail(
                    node,
                    f"{node.func.id!r} holds a value, not a function — only the host "
                    f"functions and the builtins can be called",
                )
        else:
            raise _fail(node, "a call target must be a plain name or a container method")

        if any(keyword.arg is None for keyword in node.keywords):
            raise _fail(node, "argument unpacking is not part of the script language")
        for argument in node.args:
            if isinstance(argument, ast.Starred):
                raise _fail(node, "argument unpacking is not part of the script language")
            self._check_expression(argument, depth + 1)
        for keyword in node.keywords:
            self._check_expression(keyword.value, depth + 1)

    def _check_comprehension(self, node: ast.ListComp, depth: int = 0) -> None:
        """Comprehension targets bind only inside the comprehension."""
        outer = set(self._bound)
        for generator in node.generators:
            if generator.is_async:
                raise _fail(node, "async is not part of the script language")
            self._check_expression(generator.iter, depth + 1)
            self._bind_target(generator.target)
            for condition in generator.ifs:
                self._check_expression(condition, depth + 1)
        self._check_expression(node.elt, depth + 1)
        self._bound = outer

    def _check_operator(
        self, node: ast.expr, operator: ast.AST, allowed: tuple[type[ast.AST], ...]
    ) -> None:
        if not isinstance(operator, allowed):
            raise _fail(node, f"operator {type(operator).__name__} is not supported")


__all__ = [
    "ALLOWED_BUILTINS",
    "ALLOWED_METHODS",
    "CALLABLE_NAMES",
    "HOST_FUNCTIONS",
    "MAX_EXPRESSION_DEPTH",
    "MAX_NESTING",
    "MAX_SCRIPT_BYTES",
    "PREDEFINED_NAMES",
    "ScriptSyntaxError",
    "parse_script",
]
