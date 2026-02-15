"""Calculator extension — example of registering a custom tool.

Extension files must define an ``extension(api)`` factory function.
The ExtensionAPI provides register_tool() and on() for event hooks.
"""

from __future__ import annotations

import ast
import operator
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolTransport

# Safe operators for math evaluation (no builtins, no exec)
_SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_MAX_EXPR_LEN = 500


def _safe_eval(expr: str) -> float:
    """Evaluate a math expression using AST — no eval()."""
    if len(expr) > _MAX_EXPR_LEN:
        msg = f"Expression too long ({len(expr)} chars, max {_MAX_EXPR_LEN})"
        raise ValueError(msg)

    tree = ast.parse(expr, mode="eval")

    def _eval_node(node: ast.expr) -> float:
        if isinstance(node, ast.Expression):
            return _eval_node(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp):
            op_func = _SAFE_OPS.get(type(node.op))
            if op_func is None:
                msg = f"Unsupported operator: {type(node.op).__name__}"
                raise ValueError(msg)
            return op_func(_eval_node(node.left), _eval_node(node.right))
        if isinstance(node, ast.UnaryOp):
            op_func = _SAFE_OPS.get(type(node.op))
            if op_func is None:
                msg = f"Unsupported unary operator: {type(node.op).__name__}"
                raise ValueError(msg)
            return op_func(_eval_node(node.operand))
        msg = f"Unsupported expression: {type(node).__name__}"
        raise ValueError(msg)

    return _eval_node(tree)


def extension(api: Any) -> None:
    """Register the calculator tool."""

    async def execute(expression: str) -> str:
        """Evaluate a math expression safely."""
        try:
            result = _safe_eval(expression)
            # Clean up float display (3.0 → 3)
            if result == int(result):
                return str(int(result))
            return str(result)
        except (ValueError, SyntaxError, ZeroDivisionError) as exc:
            return f"Error: {exc}"

    api.register_tool(
        RegisteredTool(
            name="calculate",
            description="Evaluate a math expression. Supports +, -, *, /, %, ** and parentheses.",
            input_schema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Math expression to evaluate (e.g. '2 + 3 * 4')",
                    },
                },
                "required": ["expression"],
            },
            transport=ToolTransport.NATIVE,
            execute=execute,
            source="extension:calculator",
        )
    )
