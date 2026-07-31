"""Architecture regression guard: arcrun must NOT import arcagent.

SDD §5 Module Boundary Rules (SPEC-018 HIGH-1):
    arcrun is the execution loop layer.
    arcagent is the agent nucleus that depends on arcrun.
    arcrun has ZERO knowledge of arcagent — the dependency flows downward.

    arcrun  ←  arcagent  ←  arcgateway

Any arcagent import inside arcrun creates a latent or actual circular
dependency (arcrun → arcagent → arcrun) and violates the "Don't Mix
Concerns" principle documented in Arc CLAUDE.md.

The trust-store primitives previously imported from ``arcagent.utils.trust_store``
inside ``arcrun.backends.loader`` were the concrete violation.  They are now
sourced from ``arctrust``, a neutral sibling package that neither arcrun nor
arcagent depends on (SPEC-018 HIGH-1 fix).

Concrete test:
    AST-scan every .py file under packages/arcrun/src/.
    Fail if any file contains ``import arcagent`` or ``from arcagent``.
"""

from __future__ import annotations

import ast
from pathlib import Path


def _find_arcagent_imports(path: Path) -> list[str]:
    """Return violation descriptions for a Python file that imports arcagent.

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
                if alias.name == "arcagent" or alias.name.startswith("arcagent."):
                    violations.append(
                        f"{path}:{node.lineno}: import {alias.name}"
                    )

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "arcagent" or module.startswith("arcagent."):
                names = ", ".join(a.name for a in node.names)
                violations.append(
                    f"{path}:{node.lineno}: from {module} import {names}"
                )

    return violations


def test_no_arcrun_imports_arcagent() -> None:
    """AST-scan arcrun source; fail if any file imports from arcagent.

    SDD §5: arcrun has zero knowledge of arcagent.
    arcagent depends on arcrun — NOT the other way around.

    Specifically: ``arcrun.backends.loader`` previously imported
    ``arcagent.utils.trust_store`` for Ed25519 verification.  This was a
    latent circular dependency that has been resolved by migrating to
    ``arctrust.trust_store`` (SPEC-018 HIGH-1).

    If this test fails, a new arcagent import was added to arcrun.  Fix by:
    1. Moving the shared concern into ``arctrust`` or another neutral package.
    2. Removing the arcagent import from arcrun source.
    3. If the dependency inversion is intentional, update SDD §5 first.
    """
    arcrun_src = (
        Path(__file__).parent.parent.parent
        / "packages"
        / "arcrun"
        / "src"
        / "arcrun"
    )
    assert arcrun_src.exists(), (
        f"arcrun source not found at {arcrun_src}. "
        "Is the packages/arcrun directory present?"
    )

    all_violations: list[str] = []

    for py_file in sorted(arcrun_src.rglob("*.py")):
        violations = _find_arcagent_imports(py_file)
        all_violations.extend(violations)

    if all_violations:
        msg = (
            "ARCHITECTURE VIOLATION: arcrun imports arcagent.\n\n"
            "arcagent depends on arcrun — NOT the other way around.\n"
            "arcrun must have ZERO knowledge of arcagent (SDD §5).\n\n"
            "Violations found:\n"
            + "\n".join(f"  {v}" for v in all_violations)
            + "\n\n"
            "To fix:\n"
            "  1. Move shared code to arctrust or another neutral package.\n"
            "  2. Remove the arcagent import from arcrun source.\n"
            "  3. If the dependency inversion is intentional, update SDD §5 first.\n"
        )
        raise AssertionError(msg)
