"""Physical-removal guards for the optional shared-knowledge layering."""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]


def _imports(source: Path) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None
    }


def test_arcmemory_collection_mechanics_have_no_fleet_dependency() -> None:
    source = (
        _ROOT / "packages" / "arcmemory" / "src" / "arcmemory" / "adapters" / "shared_knowledge.py"
    )
    assert not any(name == "arcteam" or name.startswith("arcteam.") for name in _imports(source))
    assert "for_arc_team" not in source.read_text(encoding="utf-8")


def test_arcteam_backend_has_no_static_arcmemory_dependency() -> None:
    root = _ROOT / "packages" / "arcteam" / "src" / "arcteam" / "shared_knowledge"
    for source in root.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in tree.body
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None
        }
        assert not any(name == "arcmemory" or name.startswith("arcmemory.") for name in imports)
