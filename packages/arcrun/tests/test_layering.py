"""Architecture test (SPEC-027 AC-5.1 / C.8) — arcrun is a lower layer.

arcrun owns the loop and the CapabilityProvider *Protocol*; it must never depend
on arcagent or any concrete capability/skill implementation. The dependency
arrow points one way: arcagent → arcrun, never back.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "arcrun"

# Arc sibling packages arcrun must never import (it sits below them).
_FORBIDDEN_PREFIXES = ("arcagent", "arcgateway", "arccli", "arcui", "arcteam", "arcmas")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_arcrun_imports_no_arc_sibling() -> None:
    """No arcrun source module imports arcagent/arcgateway/arccli/arcui/arcteam/arcmas."""
    offenders: list[str] = []
    for py in _SRC.rglob("*.py"):
        for mod in _imported_modules(py):
            if mod.startswith(_FORBIDDEN_PREFIXES):
                offenders.append(f"{py.relative_to(_SRC)} imports {mod}")
    assert not offenders, "arcrun must not import a higher Arc layer:\n" + "\n".join(offenders)


def test_capability_provider_is_a_protocol() -> None:
    """arcrun defines the CapabilityProvider contract itself (owns the seam)."""
    from arcrun import CapabilityProvider

    # It is a runtime-checkable Protocol, not a concrete class to subclass.
    assert getattr(CapabilityProvider, "_is_runtime_protocol", False), (
        "CapabilityProvider must be @runtime_checkable"
    )
