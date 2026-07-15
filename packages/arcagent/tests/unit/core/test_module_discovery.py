"""Folder-presence module discovery + explicit (default-off) activation.

The loader SCANS ``modules/`` for folders that qualify as a module (both
``capabilities.py`` and ``_runtime.py`` present), so the full present-set is
always KNOWN. A discovered module only *loads* when the agent's config enables
it — discovered-but-not-enabled is a valid, listable, inactive state.
"""

from __future__ import annotations

from pathlib import Path

from arcagent.core.config import ModuleEntry
from arcagent.core.module_discovery import (
    active_modules,
    discover_modules,
    module_statuses,
)


def _make_module(root: Path, name: str, *, capabilities: bool = True, runtime: bool = True) -> None:
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


def test_discovers_real_modules_shipped_in_tree() -> None:
    # The real modules/ tree contains the 18 shipped modules; every one has
    # both capabilities.py and _runtime.py, so discovery must find them all.
    discovered = discover_modules()
    for name in ("browser", "memory", "messaging", "planning", "tasks", "voice"):
        assert name in discovered


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
