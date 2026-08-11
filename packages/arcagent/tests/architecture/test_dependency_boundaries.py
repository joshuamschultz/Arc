"""Architectural dependency direction for the Arc execution stack.

The packages are independently useful layers.  Source imports and package
metadata must preserve the same one-way graph::

    arcllm <- arcrun <- arcagent <- arcgateway / arcui

ArcAgent deliberately consumes ArcRun through one obvious public facade:
``import arcrun`` followed by qualified access.  Deep imports make ArcRun's
internal file layout part of ArcAgent's contract and are forbidden here.
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Iterable
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ARCAGENT_SRC = _REPO_ROOT / "packages" / "arcagent" / "src" / "arcagent"
_ARCRUN_INIT = _REPO_ROOT / "packages" / "arcrun" / "src" / "arcrun" / "__init__.py"

_FORBIDDEN_ARCAGENT_IMPORTS = ("arcllm", "arcgateway", "arcui")
_FORBIDDEN_ARCAGENT_DEPENDENCIES = frozenset({"arcllm", "arcgateway", "arcui"})


def _python_sources(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _imports(path: Path) -> Iterable[tuple[int, str, str]]:
    """Yield ``(line, form, module)`` for every absolute import in *path*."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, "import", alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, "from", node.module


def _arcagent_boundary_violations(files: Iterable[Path]) -> list[str]:
    violations: list[str] = []
    for path in files:
        for line, form, module in _imports(path):
            if module.startswith(_FORBIDDEN_ARCAGENT_IMPORTS):
                violations.append(f"{path}:{line}: imports forbidden lower-layer bypass {module}")
            if module == "arcrun" and form == "from":
                violations.append(f"{path}:{line}: use `import arcrun`, not `from arcrun ...`")
            elif module.startswith("arcrun."):
                violations.append(f"{path}:{line}: imports ArcRun internal module {module}")
    return violations


def _dependency_name(requirement: str) -> str:
    """Return a normalized distribution name from a PEP 508 requirement."""
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
    assert match is not None, f"invalid dependency entry: {requirement!r}"
    return match.group(1).lower().replace("_", "-")


def _arcagent_dependencies(pyproject: Path) -> set[str]:
    with pyproject.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return {_dependency_name(value) for value in project.get("dependencies", [])}


def _public_arcrun_names() -> set[str]:
    tree = ast.parse(_ARCRUN_INIT.read_text(encoding="utf-8"), filename=str(_ARCRUN_INIT))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            continue
        assert isinstance(node.value, (ast.List, ast.Tuple))
        return {
            item.value
            for item in node.value.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
    raise AssertionError("arcrun.__init__ must define a literal __all__ facade")


def _qualified_arcrun_names(files: Iterable[Path]) -> set[str]:
    names: set[str] = set()
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "arcrun"
            ):
                names.add(node.attr)
    return names


def test_boundary_scanner_rejects_bypasses_and_deep_imports(tmp_path: Path) -> None:
    """Prove the architecture scanner can fail for every forbidden import form."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "import arcllm\nfrom arcgateway import GatewayRunner\nimport arcui.server\n"
        "from arcrun import run\nimport arcrun.state\n",
        encoding="utf-8",
    )

    violations = _arcagent_boundary_violations([planted])

    assert len(violations) == 5


def test_arcagent_uses_only_the_public_arcrun_facade() -> None:
    """ArcAgent imports ArcRun once and reaches public names through that module."""
    sources = _python_sources(_ARCAGENT_SRC)
    assert len(sources) > 250, "ArcAgent source discovery collapsed"

    violations = _arcagent_boundary_violations(sources)

    assert not violations, "ArcAgent dependency-boundary violations:\n" + "\n".join(violations)


def test_arcagent_metadata_has_no_lower_layer_bypass() -> None:
    """Package metadata must enforce the same graph as source imports."""
    dependencies = _arcagent_dependencies(_REPO_ROOT / "packages" / "arcagent" / "pyproject.toml")

    assert not dependencies & _FORBIDDEN_ARCAGENT_DEPENDENCIES
    assert "arcrun" in dependencies


def test_every_arcrun_name_used_by_arcagent_is_public() -> None:
    """Qualified ArcRun calls must resolve through its declared root facade."""
    used = _qualified_arcrun_names(_python_sources(_ARCAGENT_SRC))

    assert used, "ArcAgent must consume the ArcRun facade"
    assert used <= _public_arcrun_names()


@pytest.mark.parametrize("package", ["arcgateway", "arcui"])
def test_arcagent_imports_without_optional_upper_layer(package: str) -> None:
    """No ArcAgent source import may require an upper-layer package."""
    imported = {
        module.split(".", maxsplit=1)[0]
        for path in _python_sources(_ARCAGENT_SRC)
        for _, _, module in _imports(path)
    }

    assert package not in imported
