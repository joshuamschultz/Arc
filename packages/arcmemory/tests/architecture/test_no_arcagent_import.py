"""TX.1 — arcmemory sits BELOW arcagent in the DAG (REQ-001).

Import-graph assertion: no module under ``arcmemory`` may import ``arcagent`` or
``arcrun``. Parses the AST of every source file (static — does not rely on runtime
import side effects) and fails if a forbidden top-level module appears.
"""

from __future__ import annotations

import ast
from pathlib import Path

_FORBIDDEN = {"arcagent", "arcrun"}
_SRC = Path(__file__).resolve().parents[2] / "src" / "arcmemory"


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_arcmemory_imports_no_arcagent_or_arcrun() -> None:
    offenders: dict[str, set[str]] = {}
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bad = _imported_roots(tree) & _FORBIDDEN
        if bad:
            offenders[str(path.relative_to(_SRC))] = bad
    assert not offenders, f"arcmemory must not import {_FORBIDDEN}: {offenders}"


def test_detector_would_catch_a_violation() -> None:
    """The gate is real: a synthetic ``import arcagent`` is flagged."""
    tree = ast.parse("import arcagent.core\nfrom arcrun import loop\n")
    assert _imported_roots(tree) & _FORBIDDEN == _FORBIDDEN
