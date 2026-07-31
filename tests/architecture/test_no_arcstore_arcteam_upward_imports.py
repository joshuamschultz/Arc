"""Architecture regression guard: arcstore/arcteam must NOT import higher layers.

SDD §1 One-Way Dependency Direction (SPEC-056 mission-control):
    arcstore and arcteam are foundation-layer packages. The tasks feature
    makes arcstore imported by — and written to by — three higher layers
    (arcagent, arcui, arccli), which is the correct direction:

        arcagent, arcui, arccli, arcrun, arcgateway  →  arcstore, arcteam

    arcstore/arcteam must have ZERO knowledge of any layer built on top of
    them. If either package imports arcagent, arcui, arccli, arcrun, or
    arcgateway, the dependency direction has inverted and a circular
    dependency is latent (foundation → higher layer → foundation).

Concrete test:
    AST-scan every .py file under packages/arcstore/src/ and
    packages/arcteam/src/.
    Fail if any file contains ``import <layer>`` or ``from <layer>`` for
    layer in {arcagent, arcui, arccli, arcrun, arcgateway}.
"""

from __future__ import annotations

import ast
from pathlib import Path

_UPWARD_LAYERS = ("arcagent", "arcui", "arccli", "arcrun", "arcgateway")


def _find_upward_imports(path: Path) -> list[str]:
    """Return violation descriptions for a Python file importing a higher layer.

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
                if alias.name.split(".")[0] in _UPWARD_LAYERS:
                    violations.append(f"{path}:{node.lineno}: import {alias.name}")

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] in _UPWARD_LAYERS:
                names = ", ".join(a.name for a in node.names)
                violations.append(f"{path}:{node.lineno}: from {module} import {names}")

    return violations


def _scan_package(package: str) -> list[str]:
    """AST-scan a package's source tree for upward-layer imports.

    Args:
        package: Package directory name under packages/ (e.g. "arcstore").

    Returns:
        List of violation strings across all files in the package.
    """
    src = Path(__file__).parent.parent.parent / "packages" / package / "src" / package
    assert src.exists(), f"{package} source not found at {src}."

    violations: list[str] = []
    for py_file in sorted(src.rglob("*.py")):
        violations.extend(_find_upward_imports(py_file))
    return violations


def test_no_arcstore_imports_upward() -> None:
    """AST-scan arcstore source; fail if it imports arcagent/arcui/arccli/arcrun/arcgateway.

    SDD §1: arcstore is a foundation-layer package. Higher layers depend on
    it — NOT the other way around.

    If this test fails, fix by:
    1. Removing the upward import from arcstore.
    2. Moving the shared concern into arcstore itself, or a neutral
       foundation package it already depends on.
    3. If the dependency inversion is intentional, update SDD §1 first.
    """
    violations = _scan_package("arcstore")

    if violations:
        msg = (
            "ARCHITECTURE VIOLATION: arcstore imports a higher layer.\n\n"
            "arcagent/arcui/arccli/arcrun/arcgateway depend on arcstore — "
            "NOT the other way around (SDD §1).\n\n"
            "Violations found:\n" + "\n".join(f"  {v}" for v in violations) + "\n\n"
            "To fix:\n"
            "  1. Remove the upward import from arcstore.\n"
            "  2. Move shared code into arcstore or a neutral foundation package.\n"
            "  3. If the dependency inversion is intentional, update SDD §1 first.\n"
        )
        raise AssertionError(msg)


def test_no_arcteam_imports_upward() -> None:
    """AST-scan arcteam source; fail if it imports arcagent/arcui/arccli/arcrun/arcgateway.

    SDD §1: arcteam is a foundation-layer package. Higher layers depend on
    it — NOT the other way around.

    If this test fails, fix by:
    1. Removing the upward import from arcteam.
    2. Moving the shared concern into arcteam itself, or a neutral
       foundation package it already depends on.
    3. If the dependency inversion is intentional, update SDD §1 first.
    """
    violations = _scan_package("arcteam")

    if violations:
        msg = (
            "ARCHITECTURE VIOLATION: arcteam imports a higher layer.\n\n"
            "arcagent/arcui/arccli/arcrun/arcgateway depend on arcteam — "
            "NOT the other way around (SDD §1).\n\n"
            "Violations found:\n" + "\n".join(f"  {v}" for v in violations) + "\n\n"
            "To fix:\n"
            "  1. Remove the upward import from arcteam.\n"
            "  2. Move shared code into arcteam or a neutral foundation package.\n"
            "  3. If the dependency inversion is intentional, update SDD §1 first.\n"
        )
        raise AssertionError(msg)
