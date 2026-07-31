"""Architecture regression guard: the evaluations harness is a one-way leaf.

SPEC-060 §COMP-001 / REQ-174. Two edges must never appear:

    1. ``packages/`` MUST NOT import ``evaluations``.
       ``evaluations/`` is deliberately not a uv workspace member — it is a
       benchmark harness that consumes Arc, never a dependency of it. A single
       import upward would make the shipped packages depend on a directory that
       is not installed with them, and would drag benchmark code into the
       product's dependency DAG (``.claude/steering/structure.md`` Layer Model).

    2. ``evaluations/ingest/`` MUST NOT import ``evaluations/longmemeval/``.
       ``ingest/`` defines the source-agnostic seam (``SourceAdapter``,
       ``Session``, ``Turn``, ``Chunk``); ``longmemeval/`` is one consumer of
       it. If satisfying the seam required reaching into the LongMemEval
       package, it would not be a seam at all, and REQ-174's promise — a new
       corpus costs exactly one module — would be false.

Concrete test: AST-scan every .py file in each source tree and fail on any
import (absolute or relative) that crosses the forbidden edge.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent


def _is_under(module: str, forbidden: str) -> bool:
    """True when ``module`` is ``forbidden`` itself or a submodule of it."""
    return module == forbidden or module.startswith(f"{forbidden}.")


def _absolute_module(node: ast.ImportFrom, package_parts: tuple[str, ...]) -> str:
    """Resolve a ``from ... import`` target to a dotted absolute module name.

    Relative imports are resolved against the importing file's own package so
    that ``from ..longmemeval import x`` inside ``evaluations/ingest/`` is
    caught as ``evaluations.longmemeval`` rather than slipping through as the
    bare name ``longmemeval``.
    """
    if node.level == 0:
        return node.module or ""

    base = package_parts[: len(package_parts) - (node.level - 1)]
    return ".".join((*base, node.module)) if node.module else ".".join(base)


def _find_forbidden_imports(path: Path, forbidden: str, tree_root: Path) -> list[str]:
    """Return violation descriptions for imports of ``forbidden`` in one file.

    Args:
        path: Python source file to scan.
        forbidden: Dotted module prefix that must not be imported.
        tree_root: Directory the importing file's package path is rooted at,
            used to resolve relative imports.

    Returns:
        List of violation strings (empty when the file is clean).
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []  # Unparseable files are caught by the lint/type gates, not here.

    package_parts = path.relative_to(tree_root).parent.parts
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_under(alias.name, forbidden):
                    violations.append(f"{path}:{node.lineno}: import {alias.name}")

        elif isinstance(node, ast.ImportFrom):
            module = _absolute_module(node, package_parts)
            if _is_under(module, forbidden):
                names = ", ".join(alias.name for alias in node.names)
                violations.append(f"{path}:{node.lineno}: from {module} import {names}")

    return violations


def _scan(source_dir: Path, forbidden: str, tree_root: Path) -> list[str]:
    """AST-scan every .py file under ``source_dir`` for imports of ``forbidden``."""
    violations: list[str] = []
    for py_file in sorted(source_dir.rglob("*.py")):
        violations.extend(_find_forbidden_imports(py_file, forbidden, tree_root))
    return violations


def test_no_packages_import_evaluations() -> None:
    """AST-scan packages/; fail if any shipped package imports the harness.

    If this fails, product code has taken a dependency on a benchmark harness
    that is not a workspace member and is not installed with the wheels. Fix by
    moving the shared code into the appropriate package under ``packages/`` and
    having ``evaluations/`` import it downward, never the reverse.
    """
    packages = _REPO_ROOT / "packages"
    assert packages.exists(), f"packages/ not found at {packages}"

    violations = _scan(packages, forbidden="evaluations", tree_root=_REPO_ROOT)

    assert not violations, (
        "ARCHITECTURE VIOLATION: packages/ imports evaluations.\n\n"
        "evaluations/ is a benchmark harness that consumes Arc — it is not a\n"
        "uv workspace member and is not installed with the wheels.\n"
        "The dependency flows evaluations -> packages, never upward.\n\n"
        "Violations found:\n" + "\n".join(f"  {v}" for v in violations)
    )


def test_ingest_does_not_import_longmemeval() -> None:
    """AST-scan evaluations/ingest/; fail if it reaches into the corpus package.

    If this fails, the source-agnostic seam has been coupled to its first
    consumer. Fix by moving the corpus-specific piece into
    ``evaluations/longmemeval/`` and expressing what ``ingest/`` needs as part
    of the ``SourceAdapter`` contract instead.
    """
    ingest = _REPO_ROOT / "evaluations" / "ingest"
    assert ingest.exists(), f"evaluations/ingest/ not found at {ingest}"

    violations = _scan(ingest, forbidden="evaluations.longmemeval", tree_root=_REPO_ROOT)

    assert not violations, (
        "ARCHITECTURE VIOLATION: evaluations/ingest/ imports "
        "evaluations/longmemeval/.\n\n"
        "ingest/ defines the source-agnostic seam; longmemeval/ is one consumer\n"
        "of it (REQ-174). A seam that needs its consumer is not a seam.\n\n"
        "Violations found:\n" + "\n".join(f"  {v}" for v in violations)
    )
