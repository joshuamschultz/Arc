"""usearch is loaded in exactly one place, and that place loads torch first.

usearch's ``__init__`` puts NumKong's bundled ``libgomp`` into the process-wide
symbol scope (``RTLD_GLOBAL``). If torch loads after it, torch's OpenMP symbols
bind to NumKong's copy and the process segfaults (Azure ``arc ui`` crash loop).
``arcmemory.index.ann._usearch_index_type`` is the single loader that imports
torch (when installed) before usearch. A second ``import usearch`` anywhere, or
a module-level one in ``ann.py``, would bypass that order — this test fails it.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOTS = (_REPO_ROOT / "packages", _REPO_ROOT / "modules", _REPO_ROOT / "extensions")
_LOADER_FILE = _REPO_ROOT / "packages/arcmemory/src/arcmemory/index/ann.py"
_LOADER_FUNCTION = "_usearch_index_type"


def _imports_usearch(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name.split(".")[0] == "usearch" for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return (node.module or "").split(".")[0] == "usearch" and node.level == 0
    return False


def _runtime_usearch_imports(tree: ast.Module) -> list[tuple[int, str | None]]:
    """``(line, enclosing function)`` for every usearch import outside TYPE_CHECKING."""
    found: list[tuple[int, str | None]] = []

    def visit(node: ast.AST, function: str | None) -> None:
        if isinstance(node, ast.If) and ast.unparse(node.test).endswith("TYPE_CHECKING"):
            for child in node.orelse:
                visit(child, function)
            return
        if _imports_usearch(node):
            found.append((node.lineno, function))
        name = node.name if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) else function
        for child in ast.iter_child_nodes(node):
            visit(child, name)

    visit(tree, None)
    return found


def _source_files() -> list[Path]:
    return [
        path
        for root in _SOURCE_ROOTS
        if root.exists()
        for path in root.rglob("*.py")
        if "/tests/" not in path.as_posix() and ".venv" not in path.parts
    ]


def test_usearch_is_imported_only_by_the_ann_loader() -> None:
    violations = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, function in _runtime_usearch_imports(tree):
            if path != _LOADER_FILE or function != _LOADER_FUNCTION:
                where = function or "<module level>"
                violations.append(f"{path.relative_to(_REPO_ROOT)}:{line} in {where}")
    assert not violations, (
        "usearch must load only through arcmemory.index.ann._usearch_index_type, which "
        "loads torch's OpenMP runtime first:\n" + "\n".join(violations)
    )


def test_the_loader_imports_torch_before_usearch() -> None:
    tree = ast.parse(_LOADER_FILE.read_text(encoding="utf-8"))
    loader = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == _LOADER_FUNCTION
    )
    torch_loads = [
        node.lineno
        for node in ast.walk(loader)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func).endswith("import_module")
        and [ast.unparse(arg) for arg in node.args] == ["'torch'"]
    ]
    usearch_loads = [node.lineno for node in ast.walk(loader) if _imports_usearch(node)]
    assert torch_loads, "the loader no longer loads torch"
    assert usearch_loads, "the loader no longer loads usearch"
    assert max(torch_loads) < min(usearch_loads), "torch must load before usearch"
