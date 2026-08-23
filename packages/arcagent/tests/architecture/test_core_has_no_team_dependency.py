"""The ArcAgent nucleus must remain usable without the optional fleet layer."""

from __future__ import annotations

import ast
from pathlib import Path


def test_arcagent_core_has_no_arcteam_imports() -> None:
    root = Path(__file__).parents[2] / "src" / "arcagent" / "core"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert all(
                not name == "arcteam" and not name.startswith("arcteam.") for name in names
            ), path


def test_deleted_bootstrap_is_not_loadable_from_arcagent() -> None:
    assert not (
        Path(__file__).parents[2] / "src" / "arcagent" / "core" / "arcteam_bootstrap.py"
    ).exists()
