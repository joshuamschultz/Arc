"""AST-based scanner pass — detects dynamic import / eval / exec / compile.

Sibling of ``arcskill.hub.scanner``. Owns the AST visitor that catches
patterns the regex bank cannot reliably distinguish (e.g. distinguishing
``__import__("os")`` from a string literal mentioning ``__import__``).

Re-exported through ``arcskill.hub.scanner`` so callers and tests
continue to do ``from arcskill.hub.scanner import _ast_pass``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from arcskill.hub._findings import Finding


class _DangerousImportVisitor(ast.NodeVisitor):
    """Detect dynamic __import__ and importlib calls in Python AST."""

    def __init__(self) -> None:
        self.findings: list[tuple[int, str, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        """Check for __import__(), importlib.import_module(), and exec/eval."""
        self.generic_visit(node)

        func = node.func
        if isinstance(func, ast.Name):
            if func.id in ("__import__", "eval", "exec", "compile"):
                self.findings.append(
                    (
                        node.lineno,
                        f"ast_{func.id}",
                        f"AST: {func.id}() call -- dynamic execution",
                    )
                )

        elif isinstance(func, ast.Attribute):
            full = f"{_attr_name(func)}"
            if full in (
                "importlib.import_module",
                "importlib.util.spec_from_file_location",
                "importlib.util.module_from_spec",
            ):
                self.findings.append(
                    (
                        node.lineno,
                        "ast_dynamic_import",
                        f"AST: {full}() -- dynamic module loading",
                    )
                )


def _attr_name(node: ast.Attribute | ast.Name) -> str:
    """Reconstruct a dotted attribute name from an AST node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_attr_name(node.value)}.{node.attr}"  # type: ignore[arg-type]
    return "<unknown>"


def _ast_pass(root: Path) -> list[Finding]:
    """Run the custom AST visitor on all Python files."""
    findings: list[Finding] = []

    for path in root.rglob("*.py"):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, OSError):
            continue

        visitor = _DangerousImportVisitor()
        visitor.visit(tree)
        rel = str(path.relative_to(root))

        for lineno, rule_id, message in visitor.findings:
            findings.append(
                Finding(
                    severity="high",
                    category="structural",
                    rule_id=rule_id,
                    message=message,
                    path=rel,
                    line=lineno,
                )
            )

    return findings
