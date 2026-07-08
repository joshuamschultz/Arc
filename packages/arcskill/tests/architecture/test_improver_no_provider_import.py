"""T0.3 — ``arcskill.improver`` is provider-free (SPEC-044 REQ-004, D-3).

The improver is pure skill-improvement logic over injected Protocol seams. No
module under ``arcskill.improver`` may import ``arcagent``, ``arcllm``, or
``arcmemory``: those enter only through the ``Mutator``/``Judge``/``EvalRunner``/
``Signer``/``AuditSink`` seams that arcagent injects. Static AST assertion — does
not rely on runtime import side effects.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

_FORBIDDEN = {"arcagent", "arcllm", "arcmemory", "arcrun"}
_SRC = Path(__file__).resolve().parents[2] / "src" / "arcskill" / "improver"


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_improver_subpackage_exists() -> None:
    """The subpackage must exist and be importable (RED before scaffold)."""
    importlib.import_module("arcskill.improver")
    assert _SRC.is_dir(), f"expected {_SRC} to exist"


def test_improver_imports_no_provider() -> None:
    offenders: dict[str, set[str]] = {}
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bad = _imported_roots(tree) & _FORBIDDEN
        if bad:
            offenders[str(path.relative_to(_SRC))] = bad
    assert not offenders, f"arcskill.improver must not import {_FORBIDDEN}: {offenders}"


def test_detector_would_catch_a_violation() -> None:
    """The gate is real: synthetic forbidden imports are flagged."""
    tree = ast.parse("import arcagent.core\nfrom arcllm import x\nimport arcmemory\n")
    assert _imported_roots(tree) & _FORBIDDEN == {"arcagent", "arcllm", "arcmemory"}
