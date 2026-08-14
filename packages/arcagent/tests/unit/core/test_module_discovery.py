"""Folder-presence module discovery + explicit (default-off) activation.

The loader SCANS the deployment module root — :func:`arctrust.paths.module_root`
— for folders that qualify as a module (both ``capabilities.py`` and
``_runtime.py`` present), so the full present-set is always KNOWN. A discovered
module only *loads* when the agent's config enables it — discovered-but-not-enabled
is a valid, listable, inactive state.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust.paths import module_root

import arcagent
from arcagent.core.config import ModuleEntry
from arcagent.core.module_discovery import (
    active_modules,
    discover_modules,
    module_statuses,
)


def _make_module(
    root: Path, name: str, *, capabilities: bool = True, runtime: bool = True
) -> None:
    mod = root / name
    mod.mkdir(parents=True)
    (mod / "__init__.py").write_text("")
    if capabilities:
        (mod / "capabilities.py").write_text("")
    if runtime:
        (mod / "_runtime.py").write_text("")


class _Cfg:
    """Minimal stand-in exposing the ``modules`` mapping the seam reads."""

    def __init__(self, modules: dict[str, ModuleEntry]) -> None:
        self.modules = modules


def test_default_root_is_the_deployment_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SPEC-066 REQ-333/336: the scan root is where the OPERATOR installed
    # signed bundles, not wherever the package happens to be unpacked.
    _make_module(module_root(tmp_path), "widget")
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))

    assert discover_modules() == ["widget"]


def test_default_root_is_resolved_per_call_not_at_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A root captured at import time freezes whatever the environment said
    # when this module was first imported — which is long before a service
    # unit or a test sets the var.
    _make_module(module_root(tmp_path / "a"), "first")
    _make_module(module_root(tmp_path / "b"), "second")

    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "a"))
    assert discover_modules() == ["first"]
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "b"))
    assert discover_modules() == ["second"]


def test_installed_package_directory_is_never_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # REQ-336: shipping in the wheel must stop being a reason to load. Until
    # T-970 removes the source, the package still HAS a populated modules/
    # tree — so an empty deployment root has to report empty anyway.
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    shipped = Path(arcagent.__file__).resolve().parent / "modules"
    assert (shipped / "memory" / "_runtime.py").is_file(), "guard: the wheel still ships modules"

    assert discover_modules() == []


def test_discovers_folder_with_both_files(tmp_path: Path) -> None:
    _make_module(tmp_path, "widget")
    assert discover_modules(tmp_path) == ["widget"]


def test_skips_folder_missing_runtime(tmp_path: Path) -> None:
    _make_module(tmp_path, "libonly", runtime=False)
    assert discover_modules(tmp_path) == []


def test_skips_folder_missing_capabilities(tmp_path: Path) -> None:
    _make_module(tmp_path, "noplugin", capabilities=False)
    assert discover_modules(tmp_path) == []


def test_skips_underscore_prefixed_and_pycache(tmp_path: Path) -> None:
    _make_module(tmp_path, "_private")
    (tmp_path / "__pycache__").mkdir()
    assert discover_modules(tmp_path) == []


def test_discovered_but_not_enabled_is_inactive(tmp_path: Path) -> None:
    _make_module(tmp_path, "widget")
    cfg = _Cfg({})  # no [modules.widget] entry
    status = module_statuses(cfg, tmp_path)["widget"]
    assert status.discovered is True
    assert status.enabled is False
    assert active_modules(cfg, tmp_path) == []


def test_discovered_and_enabled_is_active(tmp_path: Path) -> None:
    _make_module(tmp_path, "widget")
    cfg = _Cfg({"widget": ModuleEntry(enabled=True)})
    status = module_statuses(cfg, tmp_path)["widget"]
    assert status.discovered is True
    assert status.enabled is True
    assert active_modules(cfg, tmp_path) == ["widget"]


def test_discovered_but_disabled_is_inactive(tmp_path: Path) -> None:
    _make_module(tmp_path, "widget")
    cfg = _Cfg({"widget": ModuleEntry(enabled=False)})
    assert module_statuses(cfg, tmp_path)["widget"].enabled is False
    assert active_modules(cfg, tmp_path) == []


def test_config_entry_naming_missing_folder_is_known_but_never_active(tmp_path: Path) -> None:
    # A [modules.ghost] entry with no folder on disk: surfaced as not-discovered,
    # never active (cannot load what is not present).
    cfg = _Cfg({"ghost": ModuleEntry(enabled=True)})
    status = module_statuses(cfg, tmp_path)["ghost"]
    assert status.discovered is False
    assert status.enabled is False
    assert active_modules(cfg, tmp_path) == []
