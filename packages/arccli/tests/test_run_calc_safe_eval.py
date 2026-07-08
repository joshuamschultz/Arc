"""Security regression: the `arc run --with-calc` demo tool must not eval `**`.

The tool body is ``str(_eval_arith(ast.parse(expr, mode="eval")))`` — these tests
exercise that exact path. The old implementation called ``eval(expr)`` on the
LLM-supplied expression, so ``9**9**9**9`` was an unbounded-compute/memory DoS
(LLM10). The AST walker whitelists only + - * / % and unary +/-; Pow is rejected.
"""

from __future__ import annotations

import ast

import pytest

from arccli.commands.run import _eval_arith


def _calc(expr: str) -> float:
    """Evaluate exactly as the calculate tool does."""
    return _eval_arith(ast.parse(expr, mode="eval"))


class TestSafeArithmetic:
    def test_addition(self) -> None:
        assert _calc("2 + 2") == 4

    def test_precedence_and_parens(self) -> None:
        assert _calc("(1 + 2) * 3 - 4 / 2") == 7

    def test_modulo(self) -> None:
        assert _calc("10 % 3") == 1

    def test_unary_minus(self) -> None:
        assert _calc("-5 + 2") == -3


class TestRejectsDangerousInput:
    def test_pow_operator_is_rejected(self) -> None:
        """`**` is the DoS vector — it must not evaluate."""
        with pytest.raises(ValueError, match="Unsupported operator"):
            _calc("9 ** 9")

    def test_pow_dos_expression_is_rejected(self) -> None:
        """The astronomically-large integer expression never evaluates."""
        with pytest.raises(ValueError, match="Unsupported operator"):
            _calc("9**9**9**9")

    def test_names_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unsupported expression"):
            _calc("__import__")

    def test_calls_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unsupported expression"):
            _calc("pow(9, 9)")
