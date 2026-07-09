"""Module boundary enforcement (SPEC-023 SDD §2).

These tests are AST-based — they parse the source files and inspect the
import statements. They do NOT import the modules under test, so a
forbidden import detected here is reported as a clean test failure
rather than a runtime ImportError further up the dependency chain.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ARCUI_SRC = _REPO_ROOT / "packages" / "arcui" / "src" / "arcui"
_ARCGATEWAY_SRC = _REPO_ROOT / "packages" / "arcgateway" / "src" / "arcgateway"


def _imports_in(path: Path) -> set[str]:
    """Return the set of top-level module names imported by ``path``.

    Walks the AST so we catch both ``import x`` and ``from x import y``.
    Returns the dotted prefix only — e.g. ``arcui.routes.chat_ws`` is
    counted as ``arcui``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    return names


def _all_python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _violations(root: Path, forbidden_prefixes: tuple[str, ...]) -> list[tuple[Path, str]]:
    """List files under root that import any of the forbidden module prefixes."""
    out: list[tuple[Path, str]] = []
    for path in _all_python_files(root):
        for module in _imports_in(path):
            for prefix in forbidden_prefixes:
                if module == prefix or module.startswith(prefix + "."):
                    out.append((path, module))
    return out


# ── arcui MUST NOT import arcagent ────────────────────────────────────────────


def test_arcui_does_not_import_arcagent() -> None:
    """ArcUI is a thin frontend over arcgateway — never reaches into arcagent."""
    if not _ARCUI_SRC.exists():
        pytest.skip("arcui package not found in this checkout")
    bad = _violations(_ARCUI_SRC, ("arcagent",))
    assert not bad, (
        "arcui imports arcagent (forbidden by SDD §2.2):\n"
        + "\n".join(f"  {p}: {m}" for p, m in bad)
    )


# ── arcgateway MUST NOT import arcui ──────────────────────────────────────────


def test_arcgateway_does_not_import_arcui() -> None:
    """One-way layering: arcui → arcgateway, never the reverse."""
    if not _ARCGATEWAY_SRC.exists():
        pytest.skip("arcgateway package not found in this checkout")
    bad = _violations(_ARCGATEWAY_SRC, ("arcui",))
    assert not bad, (
        "arcgateway imports arcui (forbidden by SDD §2.2):\n"
        + "\n".join(f"  {p}: {m}" for p, m in bad)
    )


# ── Adapters MUST NOT import arcui or arcagent ───────────────────────────────


def test_adapters_do_not_import_arcui_or_arcagent() -> None:
    """Adapters are platform abstractions — they consume gateway primitives only."""
    adapters_dir = _ARCGATEWAY_SRC / "adapters"
    if not adapters_dir.exists():
        pytest.skip("arcgateway.adapters not present")
    bad = _violations(adapters_dir, ("arcui", "arcagent"))
    assert not bad, (
        "adapter imports an upstream module (forbidden by SDD §2.2):\n"
        + "\n".join(f"  {p}: {m}" for p, m in bad)
    )


# ── Web adapter is a leaf — must not import bootstrap (composition root) ─────


def test_web_adapter_does_not_import_bootstrap() -> None:
    """Composition flows top-down: bootstrap → adapter, never the reverse."""
    web_path = _ARCGATEWAY_SRC / "adapters" / "web.py"
    if not web_path.exists():
        pytest.skip("web adapter not present")
    imports = _imports_in(web_path)
    assert "arcgateway.bootstrap" not in imports, (
        "WebPlatformAdapter must remain a leaf — bootstrap is its composer"
    )


# ── Gateway core is platform-agnostic ────────────────────────────────────────


def test_gateway_does_not_import_extension_packages() -> None:
    """The gateway core must never import a platform extension package.

    Platforms load only through the entry-point registry. A direct import would
    re-couple the core to a specific platform and defeat the plugin model.
    """
    if not _ARCGATEWAY_SRC.exists():
        pytest.skip("arcgateway package not found in this checkout")
    bad = _violations(
        _ARCGATEWAY_SRC,
        ("arcgateway_telegram", "arcgateway_slack", "arcgateway_mattermost"),
    )
    assert not bad, (
        "arcgateway core imports a platform extension package (forbidden):\n"
        + "\n".join(f"  {p}: {m}" for p, m in bad)
    )


def test_gateway_core_ships_no_platform_adapter_modules() -> None:
    """The only adapter in the core is ``web``; remote platforms live in packages."""
    adapters_dir = _ARCGATEWAY_SRC / "adapters"
    if not adapters_dir.exists():
        pytest.skip("arcgateway.adapters not present")
    forbidden = {"telegram.py", "slack.py", "mattermost.py", "discord.py"}
    present = {p.name for p in adapters_dir.glob("*.py")} & forbidden
    assert not present, (
        f"platform adapter modules must not live in the gateway core: {sorted(present)}"
    )
