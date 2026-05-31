"""Architecture test — arctrust is the leaf of the import DAG (FR-1 AC-1.5).

arctrust is the security nucleus: it may import only stdlib, pynacl, and
pydantic. Importing any other Arc package would invert the dependency graph
(``arctrust ← everything else``) and break ADR-019 nucleus purity.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "arctrust"


def _arc_imports(module_path: Path) -> set[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            top = name.split(".", 1)[0]
            if top.startswith("arc") and top != "arctrust":
                found.add(top)
    return found


def test_arctrust_imports_no_arc_package() -> None:
    offenders: dict[str, set[str]] = {}
    for module_path in _SRC.rglob("*.py"):
        imports = _arc_imports(module_path)
        if imports:
            offenders[str(module_path.relative_to(_SRC))] = imports
    assert not offenders, f"arctrust must import no other Arc package, found: {offenders}"


def test_emit_single_default_sink_no_ui_coupling() -> None:
    """SPEC-026 AC-5.4 — emit() fans out to one durable sink (the WORM), and
    arctrust has no UI coupling.

    The only durable sink in arctrust's public surface is ``WormSink``
    (``NullSink`` is the explicit no-op for tests). The old ``UIBridgeSink`` /
    ``JsonlSink`` / ``SignedChainSink`` are gone, so there is structurally no
    second sink — and certainly no UI sink — for ``emit()`` to fan out to.
    ``test_arctrust_imports_no_arc_package`` already proves arctrust imports no
    ``arcui``; this pins the sink surface so a future re-introduction trips here.
    """
    from arctrust import audit

    # Concrete sink classes (exclude the ``AuditSink`` structural Protocol).
    sink_names = {
        name
        for name in audit.__all__
        if name.endswith("Sink")
        and isinstance(getattr(audit, name), type)
        and name != "AuditSink"
    }
    assert sink_names == {"WormSink", "NullSink"}, (
        f"arctrust must expose exactly the WORM sink (+ NullSink no-op); found {sink_names}"
    )
