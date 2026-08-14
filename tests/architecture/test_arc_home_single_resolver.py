"""Architecture test — ``arctrust.paths`` is the ONLY Arc-home resolver.

This repo has been bitten twice by a split resolver: one surface read a
different directory than another, and a test wrote into a developer's real
``~/.arc``. The layout split (runtime / config / state / team) makes that class
of bug catastrophic rather than merely confusing — a surface that composes
``arc_home() / "operator"`` by hand keeps reading the pre-split path after a
migration, and the operator signing key silently "disappears".

So there is exactly one rule, enforced here by AST:

* **Composing off ``arc_home()``** — ``arc_home() / "trust"`` — is banned.
  Call the typed accessor (``arctrust.paths.trust_dir()``) instead.
* **Re-deriving the root** — ``Path.home() / ".arc"`` or reading
  ``ARC_CONFIG_DIR`` directly — is banned outside ``arctrust/paths.py``.

``arctrust/paths.py`` is the one exempt file: it *is* the resolver.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_PACKAGES = _REPO / "packages"

#: The resolver itself, plus the migration that must know both layouts.
_EXEMPT = {
    Path("arctrust/src/arctrust/paths.py"),
    Path("arctrust/src/arctrust/home_migration.py"),
}

_ENV_VAR = "ARC_CONFIG_DIR"

#: Leaf names that exist ONLY under the Arc home. Composing one off any variable
#: — not just off ``arc_home()`` — rebuilds a layout path by hand, which is how
#: ``arccli.operator_key_path`` kept answering the pre-split operator-key location
#: while ``arctrust`` answered the new one: ``arc init`` minted a key under one
#: path and the workflow runner looked under the other. Deliberately excludes
#: names that also appear elsewhere: in an AGENT directory (``arcagent.toml``,
#: ``capabilities``, ``skills``, ``workflows``, ``store``, ``modules``, ``team``)
#: or in the PACKAGED tree (``blueprints`` ships inside the wheel as well). Those
#: are legitimately composed off their own root.
_HOME_ONLY_LEAVES = frozenset(
    {"operator.key", "users.json", "connections.toml", "gateway.toml", "arc.env"}
)


def _source_files() -> list[Path]:
    files = [p for p in _PACKAGES.glob("*/src/**/*.py") if "/tests/" not in p.as_posix()]
    assert files, "no package sources found — the glob is wrong, not the repo"
    return files


def _relative(path: Path) -> Path:
    return path.relative_to(_PACKAGES)


def _is_exempt(path: Path) -> bool:
    return _relative(path) in _EXEMPT


def _call_name(node: ast.AST) -> str | None:
    """Return the called function's bare name for ``f()`` and ``mod.f()``."""
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _findings(tree: ast.AST, check: str) -> list[int]:
    """Line numbers where *check* is violated in one parsed module."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
            continue
        if check == "compose_arc_home" and _call_name(node.left) == "arc_home":
            hits.append(node.lineno)
        elif check == "rederive_root" and _call_name(node.left) == "home":
            # ``Path.home() / ".arc"`` — the hand-rolled root.
            right = node.right
            if isinstance(right, ast.Constant) and right.value == ".arc":
                hits.append(node.lineno)
    return hits


def _home_only_leaves(tree: ast.AST) -> list[int]:
    """Line numbers where a home-only leaf name is joined onto a path by hand."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
            continue
        right = node.right
        if isinstance(right, ast.Constant) and right.value in _HOME_ONLY_LEAVES:
            hits.append(node.lineno)
    return hits


def test_no_surface_joins_a_home_only_leaf_by_hand() -> None:
    """The generalization of the rule above, and the one that caught a real defect.

    ``arc_home() / "operator"`` is easy to spot. ``Path(arc_dir) / "operator" /
    _KEY_NAME`` is the same bug wearing a variable, and it shipped: the CLI wrote
    the operator key to the pre-split path while arctrust read the new one.
    """
    offenders: dict[str, list[int]] = {}
    for path in _source_files():
        if _is_exempt(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lines = _home_only_leaves(tree)
        if lines:
            offenders[_relative(path).as_posix()] = lines
    assert not offenders, (
        "these names live only under the Arc home — resolve them with an "
        f"arctrust.paths accessor, not by joining a string. Offenders: {offenders}"
    )


def _env_reads(tree: ast.AST) -> list[int]:
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == _ENV_VAR:
            hits.append(node.lineno)
    return hits


@pytest.mark.parametrize("check", ["compose_arc_home", "rederive_root"])
def test_no_surface_builds_its_own_arc_home_path(check: str) -> None:
    offenders: dict[str, list[int]] = {}
    for path in _source_files():
        if _is_exempt(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lines = _findings(tree, check)
        if lines:
            offenders[_relative(path).as_posix()] = lines
    assert not offenders, (
        f"{check}: compose paths through an arctrust.paths accessor, not by hand. "
        f"Offenders (file -> lines): {offenders}"
    )


def test_only_the_resolver_reads_the_env_var() -> None:
    """A second reader of ``ARC_CONFIG_DIR`` is a second resolver by another name.

    Docstrings legitimately *name* the variable, so only executable string
    constants count — ``ast.get_docstring`` values are stripped first.
    """
    offenders: dict[str, list[int]] = {}
    for path in _source_files():
        if _is_exempt(path):
            continue
        text = path.read_text(encoding="utf-8")
        if _ENV_VAR not in text:
            continue
        tree = ast.parse(text, filename=str(path))
        _strip_docstrings(tree)
        lines = _env_reads(tree)
        if lines:
            offenders[_relative(path).as_posix()] = lines
    assert not offenders, (
        f"{_ENV_VAR} must be read only by arctrust.paths — call an accessor instead. "
        f"Offenders (file -> lines): {offenders}"
    )


def _strip_docstrings(tree: ast.AST) -> None:
    """Blank out docstring nodes so prose mentioning the env var is not a finding."""
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = node.body
        if not body or not isinstance(body[0], ast.Expr):
            continue
        first = body[0].value
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            first.value = ""


def test_the_exempt_list_stays_tiny() -> None:
    """Every exemption is a licensed second reader. Two is the budget."""
    assert len(_EXEMPT) == 2
    for rel in _EXEMPT:
        assert (_PACKAGES / rel).is_file(), f"stale exemption: {rel}"
