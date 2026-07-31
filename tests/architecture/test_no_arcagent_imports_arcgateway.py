"""Architecture regression guard: arcagent must NOT import arcgateway.

SDD §5 Module Boundary Rules:
    arcgateway depends on arcagent (it is "the daemon that runs ArcAgents").
    arcagent has ZERO knowledge of arcgateway.
    This is a one-way dependency enforced as an architecture test.

Concrete test (SDD §5, TX.1.2):
    AST-scan every .py file under packages/arcagent/.
    Fail if any file contains ``import arcgateway`` or ``from arcgateway``.

Why this matters:
    If arcagent imports arcgateway, we have a circular dependency:
        arcgateway → arc-agent → arcgateway (cycle)
    This would prevent either package from being installed independently,
    break the "arcagent works standalone" property, and violate the
    principled separation-of-concerns documented in Arc CLAUDE.md.

This test runs on every CI build (TX.1.2 in PLAN.md).
"""

from __future__ import annotations

import ast
from pathlib import Path


def _find_arcgateway_imports(path: Path) -> list[str]:
    """Return violation descriptions for a Python file that imports arcgateway.

    Args:
        path: Path to a Python source file.

    Returns:
        List of violation strings (empty if no violations found).
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []  # Skip unparseable files — syntax errors caught elsewhere

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "arcgateway" or alias.name.startswith("arcgateway."):
                    violations.append(f"{path}:{node.lineno}: import {alias.name}")

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "arcgateway" or module.startswith("arcgateway."):
                names = ", ".join(a.name for a in node.names)
                violations.append(f"{path}:{node.lineno}: from {module} import {names}")

    return violations


def test_no_arcagent_imports_arcgateway() -> None:
    """AST-scan arcagent source; fail if any file imports from arcgateway.

    SDD §5: arcagent has zero knowledge of arcgateway.
    arcgateway depends on arcagent — NOT the other way around.

    If this test fails, the one-way dependency has been violated. Fix by:
    1. Removing the arcgateway import from arcagent.
    2. Moving the shared concern into a neutral location (e.g., arcagent utils).
    3. Or rethinking the abstraction — if arcagent genuinely needs gateway
       concepts, revisit the boundary definition in SDD §5.
    """
    arcagent_src = (
        Path(__file__).parent.parent.parent / "packages" / "arcagent" / "src" / "arcagent"
    )
    assert arcagent_src.exists(), (
        f"arcagent source not found at {arcagent_src}. Is the packages/arcagent directory present?"
    )

    all_violations: list[str] = []

    for py_file in sorted(arcagent_src.rglob("*.py")):
        violations = _find_arcgateway_imports(py_file)
        all_violations.extend(violations)

    if all_violations:
        msg = (
            "ARCHITECTURE VIOLATION: arcagent imports arcgateway.\n\n"
            "arcgateway depends on arcagent — NOT the other way around.\n"
            "arcagent must have ZERO knowledge of arcgateway (SDD §5).\n\n"
            "Violations found:\n" + "\n".join(f"  {v}" for v in all_violations) + "\n\n"
            "To fix:\n"
            "  1. Remove the arcgateway import from arcagent.\n"
            "  2. Move shared code to a neutral location if needed.\n"
            "  3. If the dependency inversion is intentional, update SDD §5 first.\n"
        )
        raise AssertionError(msg)
