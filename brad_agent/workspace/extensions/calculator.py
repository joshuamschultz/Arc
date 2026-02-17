"""Extension: calculator

Registers a safe math calculator tool with ArcAgent.
"""

from __future__ import annotations

import ast
import operator


def extension(api):
    """Factory function called by ExtensionLoader."""
    from arcrun import Tool, ToolContext

    _OPS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    def _safe_eval(node: ast.AST) -> float:
        """Recursively evaluate an AST math expression."""
        if isinstance(node, ast.Expression):
            return _safe_eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp):
            op_fn = _OPS.get(type(node.op))
            if op_fn is None:
                raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
            return op_fn(_safe_eval(node.left), _safe_eval(node.right))
        if isinstance(node, ast.UnaryOp):
            op_fn = _OPS.get(type(node.op))
            if op_fn is None:
                raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
            return op_fn(_safe_eval(node.operand))
        raise ValueError(f"Unsupported expression: {ast.dump(node)}")

    async def calculate(params: dict, ctx: ToolContext) -> str:
        """Evaluate a math expression safely using AST parsing."""
        expr = params["expression"]
        try:
            tree = ast.parse(expr, mode="eval")
            result = _safe_eval(tree)
            return str(result)
        except Exception as e:
            return f"Error: {e}"

    api.register_tool(
        Tool(
            name="calculate",
            description="Evaluate a math expression. Supports +, -, *, /, %, **.",
            input_schema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Math expression to evaluate",
                    },
                },
                "required": ["expression"],
            },
            execute=calculate,
        )
    )
