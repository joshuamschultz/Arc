from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2] / "src" / "arcagent"


def test_connected_data_has_no_reverse_or_vendor_dependencies() -> None:
    files = [ROOT / "connected_data.py", *((ROOT / "modules" / "connected_data").glob("*.py"))]
    forbidden = ("arcstore", "arcmemory", "arcagent.core", "dropbox", "google", "microsoft")
    for path in files:
        tree = ast.parse(path.read_text())
        imports = [
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        ]
        imports.extend(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not any(
            name.startswith(forbidden) and name != "arcagent.core.module_config"
            for name in imports
        ), (path, imports)


def test_connected_data_module_is_optional() -> None:
    assert (ROOT / "modules" / "connected_data" / "__init__.py").is_file()
