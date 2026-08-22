from __future__ import annotations

import ast
from pathlib import Path

_SOURCE = Path(__file__).parents[2] / "src" / "arcstore" / "inbox.py"


def test_inbox_domain_has_no_database_or_cross_layer_imports() -> None:
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    imports = {
        alias.name.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
        for alias in node.names
    )
    assert "sqlite3" not in imports
    assert "arcagent" not in imports
    assert "arcui" not in imports
    assert "arcgateway" not in imports


def test_inbox_domain_contains_no_session_projection_or_untyped_metadata() -> None:
    source = _SOURCE.read_text(encoding="utf-8")
    assert "SessionIndex" not in source
    assert "session_age" not in source
    assert "metadata: dict" not in source
    assert "InMemoryInboxRepository" not in source
