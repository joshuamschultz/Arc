"""SPEC-034 T-603 — arctrust import-boundary guard.

arctrust is a foundation package: it must import NONE of its siblings
(arcagent / arcllm / arcrun / arcteam / arcskill). A policy layer that reaches
into a sibling to fetch state would break the "pure comparator over injected
state" contract (REQ-003, REQ-008, REQ-012). This static scan fails loudly if
any such import is introduced.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
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


def test_core_import_survives_absent_vault_leaves_and_httpx(tmp_path: Path) -> None:
    package = tmp_path / "arctrust"
    shutil.copytree(
        _SRC,
        package,
        ignore=shutil.ignore_patterns("vault_anchor.py", "transit_http.py", "__pycache__"),
    )
    script = """
import builtins
import sys
original = builtins.__import__
def unavailable(name, *args, **kwargs):
    if name == 'httpx' or name in {'arctrust.vault_anchor', 'arctrust.transit_http'}:
        raise ModuleNotFoundError(name)
    return original(name, *args, **kwargs)
builtins.__import__ = unavailable
import arctrust
assert arctrust.MonotonicAnchor
assert arctrust.__file__.startswith(sys.argv[1])
assert 'httpx' not in sys.modules
assert 'arctrust.vault_anchor' not in sys.modules
assert 'arctrust.transit_http' not in sys.modules
"""
    environment = {**os.environ, "PYTHONPATH": str(tmp_path)}
    result = subprocess.run(  # noqa: S603 - fixed interpreter and literal script
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
