"""T-070 invariant — arcmemory REUSES arctrust's classification comparator.

The no-read-up gate is the SAME Bell-LaPadula predicate SPEC-038 established in
``arctrust.classification``. arcmemory must import ``dominates`` /
``parse_classification`` from arctrust and define **no comparator of its own**
(no second ladder, no re-implemented ``>=`` over classification labels). This
proves the reuse two ways: runtime identity (the symbols are arctrust's), and a
static AST scan (no local ``def dominates`` / ``def parse_classification``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import arctrust

import arcmemory.security as sec

_SRC = Path(__file__).resolve().parents[2] / "src" / "arcmemory"
_COMPARATOR_NAMES = {"dominates", "parse_classification"}


def test_security_reexports_arctrusts_comparator_by_identity() -> None:
    """The gate's comparator symbols ARE arctrust's — imported, not redefined."""
    assert sec.dominates is arctrust.dominates
    assert sec.parse_classification is arctrust.parse_classification


def test_no_arcmemory_module_defines_its_own_comparator() -> None:
    """Static scan: no arcmemory source defines a classification comparator."""
    offenders: dict[str, set[str]] = {}
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        clash = defined & _COMPARATOR_NAMES
        if clash:
            offenders[str(path.relative_to(_SRC))] = clash
    assert not offenders, f"arcmemory must not define its own comparator: {offenders}"
