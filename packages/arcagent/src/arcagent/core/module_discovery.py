"""Folder-presence discovery of the modules under :mod:`arcagent.modules`.

A *module* is a folder that ships both a ``capabilities.py`` (the tools/hooks
the loader scans) and a ``_runtime.py`` (the per-agent state configured at
startup). Discovery scans the ``modules/`` directory for such folders so the
full present-set is always KNOWN — no folder can silently contribute nothing
because it lacks a config entry.

Activation is separate and explicit (product-owner decision, DEFAULT OFF): a
discovered module *loads* only when the agent's config carries an enabled
``[modules.NAME]`` entry. Discovered-but-not-enabled is a valid, listable,
inactive state; a config entry naming a folder that is not present is surfaced
as not-discovered and never loads.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# The shipped modules live next to this package: ``arcagent/modules``.
_MODULES_DIR = Path(__file__).parent.parent / "modules"


class _ModuleEntry(Protocol):
    """Structural view of a module config entry: only ``enabled`` is read."""

    enabled: bool


class _HasModules(Protocol):
    """Structural view of config: only the ``modules`` mapping is read."""

    @property
    def modules(self) -> Mapping[str, _ModuleEntry]: ...


def _is_module(path: Path) -> bool:
    """A folder qualifies as a module iff it ships capabilities + runtime.

    Underscore-prefixed folders (``__pycache__``, framework internals) never
    qualify, so scanning is safe even as loose framework files land alongside
    the module folders.
    """
    return (
        path.is_dir()
        and not path.name.startswith("_")
        and (path / "capabilities.py").is_file()
        and (path / "_runtime.py").is_file()
    )


def discover_modules(modules_dir: Path = _MODULES_DIR) -> list[str]:
    """Return the sorted names of every module folder present on disk."""
    if not modules_dir.is_dir():
        return []
    return sorted(p.name for p in modules_dir.iterdir() if _is_module(p))


@dataclass(frozen=True)
class ModuleStatus:
    """The known state of one module name.

    ``discovered`` — a qualifying folder is present on disk.
    ``enabled`` — config enables it AND it is discovered (i.e. it will load).
    """

    name: str
    discovered: bool
    enabled: bool


def module_statuses(
    config: _HasModules, modules_dir: Path = _MODULES_DIR
) -> dict[str, ModuleStatus]:
    """Return the status of every known module name, keyed by name.

    The known set is the union of discovered folders and configured entries, so
    both a present-but-unconfigured folder and a configured-but-absent entry are
    visible (the latter with ``discovered=False``).
    """
    discovered = set(discover_modules(modules_dir))
    statuses: dict[str, ModuleStatus] = {}
    for name in sorted(discovered | set(config.modules)):
        is_discovered = name in discovered
        entry = config.modules.get(name)
        entry_enabled = entry is not None and entry.enabled
        statuses[name] = ModuleStatus(
            name=name,
            discovered=is_discovered,
            enabled=is_discovered and entry_enabled,
        )
    return statuses


def active_modules(config: _HasModules, modules_dir: Path = _MODULES_DIR) -> list[str]:
    """Return the sorted names of modules that are discovered AND enabled.

    This is exactly the set the loader configures and scans — the single seam
    that both the real load path and any listing surface agree on.
    """
    statuses = module_statuses(config, modules_dir)
    return [name for name, status in statuses.items() if status.enabled]
