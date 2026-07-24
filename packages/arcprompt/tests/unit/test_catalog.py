"""COMP-005 / REQ-134,126: catalog discovery across installed packages."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from arcprompt.catalog import PromptCatalog, _package_context_dir, load_stock
from arcprompt.document import render_prompt
from arcprompt.errors import PromptMissing


def _make_installed_package(root: Path, package: str, prompts: dict[str, str]) -> None:
    pkg_dir = root / package
    (pkg_dir).mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    context = pkg_dir / "context"
    context.mkdir()
    for name, body in prompts.items():
        raw = render_prompt(body, name=name, description=f"{name} d")
        (context / f"{name}.md").write_bytes(raw)


def test_discovers_context_dirs_and_lists_prompts_without_overlays(
    tmp_path: Path, monkeypatch: object
) -> None:
    _make_installed_package(tmp_path, "faketestpkg", {"alpha": "A body", "beta": "B body"})
    sys.path.insert(0, str(tmp_path))
    try:
        import importlib

        importlib.invalidate_caches()
        refs = PromptCatalog(packages=["faketestpkg"]).catalog()
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("faketestpkg", None)

    names = {r.name for r in refs}
    assert names == {"alpha", "beta"}
    assert all(r.package == "faketestpkg" for r in refs)
    assert all(r.stock_path.is_file() for r in refs)


def test_uninstalled_package_is_skipped_silently() -> None:
    refs = PromptCatalog(packages=["definitely_not_installed_pkg_xyz"]).catalog()
    assert refs == []


def test_package_with_no_context_dir_returns_none() -> None:
    # arcprompt itself ships no context/ dir — a real installed package without prompts.
    assert _package_context_dir("arcprompt") is None


def test_dotted_name_whose_parent_is_absent_is_skipped() -> None:
    # find_spec on a submodule of a nonexistent parent raises ImportError → skipped.
    assert _package_context_dir("definitely_absent_parent_xyz.submodule") is None


def test_stock_path_resolves_named_prompt(tmp_path: Path) -> None:
    _make_installed_package(tmp_path, "faketestpkg2", {"gamma": "G body"})
    sys.path.insert(0, str(tmp_path))
    try:
        importlib.invalidate_caches()
        catalog = PromptCatalog(packages=["faketestpkg2"])
        assert catalog.stock_path("faketestpkg2", "gamma") is not None
        assert catalog.stock_path("faketestpkg2", "missing") is None
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("faketestpkg2", None)


@pytest.mark.parametrize("bad", ["../../../etc/passwd", "..", "a/b", "x\\y", "", "n\x00ul"])
def test_stock_lookups_refuse_traversal(bad: str) -> None:
    """SEC-04: stock_path + load_stock refuse a non-component name, never touch the fs."""
    catalog = PromptCatalog(packages=["arcrun"])
    with pytest.raises(PromptMissing):
        catalog.stock_path("arcrun", bad)
    with pytest.raises(PromptMissing):
        load_stock("arcrun", bad)


def test_stock_path_on_uninstalled_package_is_none() -> None:
    assert PromptCatalog(packages=["nope_pkg"]).stock_path("nope_pkg", "x") is None


def test_load_stock_returns_body_and_raises_when_absent(tmp_path: Path) -> None:
    _make_installed_package(tmp_path, "faketestpkg3", {"delta": "D body verbatim"})
    sys.path.insert(0, str(tmp_path))
    try:
        importlib.invalidate_caches()
        assert load_stock("faketestpkg3", "delta") == "D body verbatim"
        with pytest.raises(PromptMissing):
            load_stock("faketestpkg3", "nope")
        with pytest.raises(PromptMissing):
            load_stock("uninstalled_pkg_zzz", "delta")
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("faketestpkg3", None)
