"""TX.1 — arcmemory sits BELOW arcagent in the DAG (REQ-001).

Import-graph assertion. ``arcagent`` is the hard DAG boundary: NO module under
``arcmemory`` may import it, ever. ``arcrun`` is an additive, guarded dependency
(the agentic consolidation engine) — allowed, but CONFINED to a single adapter
(``react_adapter.py``) so a future harness is a sibling adapter, not a package-wide
refactor. Parses the AST of every source file (static) and fails on a violation.
"""

from __future__ import annotations

import ast
from pathlib import Path

# arcagent is forbidden everywhere. arcrun is forbidden everywhere EXCEPT the one
# adapter that owns the whole arcrun seam.
_FORBIDDEN_ALWAYS = {"arcagent"}
_ARCRUN = "arcrun"
_ARCRUN_ADAPTER = "react_adapter.py"
_SRC = Path(__file__).resolve().parents[2] / "src" / "arcmemory"


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_arcmemory_never_imports_arcagent() -> None:
    offenders: dict[str, set[str]] = {}
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bad = _imported_roots(tree) & _FORBIDDEN_ALWAYS
        if bad:
            offenders[str(path.relative_to(_SRC))] = bad
    assert not offenders, f"arcmemory must not import {_FORBIDDEN_ALWAYS}: {offenders}"


def test_arcrun_is_confined_to_the_single_adapter() -> None:
    """arcrun may be imported ONLY by the one adapter module (coordinator directive)."""
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        if path.name == _ARCRUN_ADAPTER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _ARCRUN in _imported_roots(tree):
            offenders.append(str(path.relative_to(_SRC)))
    assert not offenders, f"arcrun must stay confined to {_ARCRUN_ADAPTER}: {offenders}"


def test_detector_would_catch_a_violation() -> None:
    """The gate is real: a synthetic ``import arcagent`` is flagged."""
    tree = ast.parse("import arcagent.core\nfrom arcrun import loop\n")
    assert _imported_roots(tree) & _FORBIDDEN_ALWAYS == _FORBIDDEN_ALWAYS
    assert _ARCRUN in _imported_roots(tree)
