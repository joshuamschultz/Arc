"""Runtime consumers must use the injected ArcStore backend seam."""

from __future__ import annotations

import ast
from pathlib import Path

_FORBIDDEN_MODULES = frozenset({"arcstore.backends.sqlite"})
_FORBIDDEN_NAMES = frozenset({"SqliteBackend", "store_db_path"})
_RUNTIME_PACKAGES = ("arcagent", "arccli", "arcgateway", "arcui")


def test_runtime_has_no_direct_sqlite_arcstore_consumers() -> None:
    """Every runtime package must resolve one configured backend at composition."""
    repository = Path(__file__).parents[4]
    violations: list[str] = []
    for package in _RUNTIME_PACKAGES:
        source_root = repository / "packages" / package / "src"
        for path in sorted(source_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in _FORBIDDEN_MODULES:
                            violations.append(f"{path}:{node.lineno}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.module in _FORBIDDEN_MODULES:
                        violations.append(f"{path}:{node.lineno}: from {node.module}")
                    for alias in node.names:
                        if alias.name in _FORBIDDEN_NAMES:
                            violations.append(f"{path}:{node.lineno}: name {alias.name}")
    assert not violations, "direct ArcStore SQLite consumers remain:\n" + "\n".join(violations)


def test_arcagent_core_keeps_arcstore_optional() -> None:
    """Core can import without the optional ArcStore package installed."""
    repository = Path(__file__).parents[4]
    violations: list[str] = []
    for path in sorted(
        (repository / "packages" / "arcagent" / "src" / "arcagent" / "core").rglob("*.py")
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.Import) and any(
                alias.name == "arcstore" or alias.name.startswith("arcstore.")
                for alias in node.names
            ):
                violations.append(f"{path}:{node.lineno}: import arcstore")
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("arcstore")
            ):
                violations.append(f"{path}:{node.lineno}: from {node.module}")
    assert not violations, "arcagent core imports optional ArcStore:\n" + "\n".join(violations)
