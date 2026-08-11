"""Repository-wide import contracts for the core execution stack."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PACKAGES_ROOT = _REPO_ROOT / "packages"
_CORE_IMPORTS = frozenset({"arcllm", "arcrun", "arcagent"})
_PUBLIC_EXTENSION_IMPORTS = frozenset(
    {
        # Optional, intentionally public backend-extension API. ArcSkill works
        # without ArcRun installed and loads this integration opportunistically.
        ("arcskill", "arcrun.backends"),
    }
)

# Dependencies flow from right to left.  A package may import only the layer
# immediately below it; arcllm is independently usable and imports none above.
_FORBIDDEN_BY_PACKAGE = {
    "arcllm": frozenset({"arcrun", "arcagent", "arcgateway", "arcui"}),
    "arcrun": frozenset({"arcagent", "arcgateway", "arcui"}),
    "arcagent": frozenset({"arcllm", "arcgateway", "arcui"}),
    "arcgateway": frozenset({"arcllm", "arcrun", "arcui"}),
    "arcui": frozenset({"arcllm", "arcrun"}),
}


@dataclass(frozen=True)
class _Import:
    path: Path
    line: int
    owner: str
    form: str
    module: str
    alias: str | None


def _production_sources() -> Iterable[tuple[str, Path]]:
    for package_dir in sorted(_PACKAGES_ROOT.iterdir()):
        source_dir = package_dir / "src"
        if not source_dir.is_dir():
            continue
        for path in sorted(source_dir.rglob("*.py")):
            yield package_dir.name, path


def _imports(owner: str, path: Path) -> Iterable[_Import]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield _Import(path, node.lineno, owner, "import", alias.name, alias.asname)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield _Import(path, node.lineno, owner, "from", node.module, None)


def _all_production_imports() -> list[_Import]:
    return [
        imported for owner, path in _production_sources() for imported in _imports(owner, path)
    ]


def _clean_import_violation(imported: _Import) -> str | None:
    target = imported.module.split(".", maxsplit=1)[0]
    if target not in _CORE_IMPORTS or target == imported.owner:
        return None
    if (imported.owner, imported.module) in _PUBLIC_EXTENSION_IMPORTS:
        return None
    if imported.form != "import" or imported.module != target or imported.alias is not None:
        return (
            f"{imported.path}:{imported.line}: cross-package {target} access must use "
            f"exactly `import {target}` and qualified public names"
        )
    return None


def _direction_violation(imported: _Import) -> str | None:
    target = imported.module.split(".", maxsplit=1)[0]
    if target in _FORBIDDEN_BY_PACKAGE.get(imported.owner, frozenset()):
        return (
            f"{imported.path}:{imported.line}: {imported.owner} must not import "
            f"{target} in arcllm <- arcrun <- arcagent <- arcgateway/arcui"
        )
    return None


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from arcrun import run", "exactly `import arcrun`"),
        ("import arcrun.state", "exactly `import arcrun`"),
        ("import arcrun as runtime", "exactly `import arcrun`"),
        ("from arcagent.core import agent", "exactly `import arcagent`"),
        ("from arcllm.types import Message", "exactly `import arcllm`"),
    ],
)
def test_clean_import_scanner_rejects_non_facade_forms(
    tmp_path: Path, source: str, expected: str
) -> None:
    planted = tmp_path / "consumer.py"
    planted.write_text(source, encoding="utf-8")
    [imported] = list(_imports("consumer", planted))

    assert expected in (_clean_import_violation(imported) or "")


def test_core_packages_are_consumed_through_root_facades() -> None:
    imports = _all_production_imports()
    assert len(imports) > 1_000, "production source discovery collapsed"
    violations = [
        violation
        for imported in imports
        if (violation := _clean_import_violation(imported)) is not None
    ]

    assert not violations, "Core-package import violations:\n" + "\n".join(violations)


def test_execution_stack_dependency_direction() -> None:
    violations = [
        violation
        for imported in _all_production_imports()
        if (violation := _direction_violation(imported)) is not None
    ]

    assert not violations, "Execution-stack direction violations:\n" + "\n".join(violations)


def test_direction_scanner_rejects_layer_bypasses(tmp_path: Path) -> None:
    planted = tmp_path / "agent.py"
    planted.write_text("import arcllm\n", encoding="utf-8")
    [imported] = list(_imports("arcagent", planted))

    assert _direction_violation(imported) is not None
