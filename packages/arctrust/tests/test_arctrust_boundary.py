"""SPEC-034 T-603 — arctrust import-boundary guard.

arctrust is a foundation package: it must import NONE of its siblings
(arcagent / arcllm / arcrun / arcteam / arcskill). A policy layer that reaches
into a sibling to fetch state would break the "pure comparator over injected
state" contract (REQ-003, REQ-008, REQ-012). This static scan fails loudly if
any such import is introduced.
"""

from __future__ import annotations

import ast
from pathlib import Path

_FORBIDDEN_ROOTS = {"arcagent", "arcllm", "arcrun", "arcteam", "arcskill"}
_SRC = Path(__file__).resolve().parent.parent / "src" / "arctrust"


def _iter_imports(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            modules.append(node.module)
    return modules


def test_arctrust_imports_no_sibling_packages() -> None:
    offenders: list[str] = []
    for py in _SRC.rglob("*.py"):
        tree = ast.parse(py.read_text(), filename=str(py))
        for module in _iter_imports(tree):
            root = module.split(".", 1)[0]
            if root in _FORBIDDEN_ROOTS:
                offenders.append(f"{py.relative_to(_SRC)}: imports {module}")
    assert not offenders, "arctrust must not import siblings:\n" + "\n".join(offenders)
