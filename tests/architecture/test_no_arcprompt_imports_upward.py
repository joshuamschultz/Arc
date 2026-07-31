"""Architecture regression guard: arcprompt is a leaf — it imports ONLY arctrust.

editable-system-prompts SDD (COMP-013, REQ-121): arcprompt sits at arctrust's
level in the dependency DAG. It owns prompt storage/resolution for every Arc
package and is imported by arcrun, arcagent, arcmemory, and arcskill — so it
must never import upward, or the dependency direction inverts and a circular
dependency is latent (leaf -> higher layer -> leaf).

Leaf-ness is not generically enforced anywhere, so this hand-written guard is
the enforcement (the PRD's "new leaf package requires a hand-written import
guard" risk). arcprompt may import arctrust and stdlib/third-party only; any
``import arc*`` other than arctrust is a violation.

Concrete test: AST-scan every .py under packages/arcprompt/src/ and fail if any
file imports an Arc package other than arctrust.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ALLOWED_ARC_IMPORTS = frozenset({"arctrust", "arcprompt"})


def _find_forbidden_arc_imports(path: Path) -> list[str]:
    """Return violation descriptions for a file importing a non-arctrust Arc package."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top.startswith("arc") and top not in _ALLOWED_ARC_IMPORTS:
                    violations.append(f"{path}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top = module.split(".")[0]
            if top.startswith("arc") and top not in _ALLOWED_ARC_IMPORTS:
                names = ", ".join(a.name for a in node.names)
                violations.append(f"{path}:{node.lineno}: from {module} import {names}")
    return violations


def test_arcprompt_imports_only_arctrust() -> None:
    """AST-scan arcprompt source; fail if it imports any Arc package but arctrust.

    If this fails, fix by removing the upward import — a prompt consumer
    (arcrun/arcagent/arcmemory/arcskill) depends on arcprompt, never the reverse.
    Move any shared concern into arctrust or arcprompt itself.
    """
    src = Path(__file__).parent.parent.parent / "packages" / "arcprompt" / "src" / "arcprompt"
    assert src.exists(), f"arcprompt source not found at {src}."

    violations: list[str] = []
    for py_file in sorted(src.rglob("*.py")):
        violations.extend(_find_forbidden_arc_imports(py_file))

    assert not violations, (
        "ARCHITECTURE VIOLATION: arcprompt is a leaf package that may import ONLY arctrust.\n\n"
        "arcrun/arcagent/arcmemory/arcskill depend on arcprompt — not the other way around "
        "(editable-system-prompts SDD, COMP-013).\n\n"
        "Violations found:\n" + "\n".join(f"  {v}" for v in violations)
    )
