"""SPEC-047 — pure-read inspection of every extension-point family (REQ-030/031).

``inspect_extensions(config, registry=None)`` reports, per family: what is selected/loaded,
whether it is available (importable / allowlisted / discovered), and its signed status.
Pure read, no side effects — safe against a booted agent or a config-only context. This is
the data the ``arc ext inspect`` / ``arc ext verify`` CLI renders (AC-6).

select-one availability is probed WITHOUT importing a BYO module: a builtin is checked with
``importlib.util.find_spec`` (no execution); a BYO dotted path is judged only by the operator
allowlist gate that ``select_extension`` enforces (importing to "check" would be the very RCE
the gate prevents — ASI04).
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from arcagent.capabilities.artifact_signing import verify_file
from arcagent.extension.families import FAMILIES, ScanManyFamily, SelectOneFamily

_NULL_CHOICES = frozenset({"none", "", "null"})


@dataclass(frozen=True)
class ExtensionStatus:
    """One inspection row: a select-one family, or one discovered scan-many capability."""

    family: str
    kind: str  # "select_one" | "scan_many"
    selected: str
    available: bool
    # "n/a" | "builtin" | "allowlisted" | "unsigned(personal)" | "refused" | "signed" | "unsigned"
    signed: str
    detail: str = ""


def inspect_extensions(config: Any, registry: Any = None) -> list[ExtensionStatus]:
    """Return the current selected/available/signed state across all four families."""
    tier = _read_tier(config)
    rows: list[ExtensionStatus] = []
    for family in FAMILIES:
        if isinstance(family, SelectOneFamily):
            rows.append(_inspect_select_one(family, config, tier))
        elif isinstance(family, ScanManyFamily) and registry is not None:
            rows.extend(_inspect_scan_many(family, registry))
    return rows


def _read_tier(config: Any) -> str:
    try:
        return str(config.security.tier)
    except AttributeError:
        return "personal"


def _module_cfg(config: Any, module_name: str) -> dict[str, Any]:
    entry = getattr(config, "modules", {}).get(module_name)
    if entry is None:
        return {}
    cfg = getattr(entry, "config", {})
    return dict(cfg) if isinstance(cfg, dict) else {}


def _inspect_select_one(family: SelectOneFamily, config: Any, tier: str) -> ExtensionStatus:
    """Report the configured select-one setting without importing a BYO module."""
    module_name, key = family.setting_path
    cfg = _module_cfg(config, module_name)
    setting = str(cfg.get(key, "none")).strip()
    allowlist = tuple(cfg.get(family.allowlist_key, ()) or ())

    if setting in _NULL_CHOICES:
        return ExtensionStatus(family.name, "select_one", setting or "none", True, "n/a")
    if setting in family.point.builtin_modules:
        module = family.point.builtin_modules[setting]
        available = _builtin_importable(module)
        return ExtensionStatus(
            family.name, "select_one", setting, available, "builtin", detail=module
        )
    # BYO dotted path — judged by the allowlist gate only (never imported to "check").
    if tier == "personal":
        return ExtensionStatus(family.name, "select_one", setting, True, "unsigned(personal)")
    if setting in allowlist:
        return ExtensionStatus(family.name, "select_one", setting, True, "allowlisted")
    return ExtensionStatus(family.name, "select_one", setting, False, "refused")


def _builtin_importable(module_name: str) -> bool:
    """True if the builtin module can be found — WITHOUT importing/executing it."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def _inspect_scan_many(family: ScanManyFamily, registry: Any) -> list[ExtensionStatus]:
    """Enumerate discovered capabilities of the family's kinds from the live registry."""
    rows: list[ExtensionStatus] = []
    for name, source_path, scan_root in _iter_registry(family.kinds, registry):
        rows.append(
            ExtensionStatus(
                family.name,
                "scan_many",
                name,
                True,
                _signed_status(source_path),
                detail=scan_root,
            )
        )
    return rows


def _iter_registry(kinds: frozenset[str], registry: Any) -> Iterator[tuple[str, Any, str]]:
    """Yield (name, source_path, scan_root) for registry entries of the given kinds.

    Reads the registry's kind-discriminated dicts directly (a snapshot read, the same
    private-dict pattern ``modules/skills/_runtime`` uses) — inspection never mutates.
    """
    sources: dict[str, dict[str, Any]] = {
        "tool": getattr(registry, "_tools", {}),
        "background_task": getattr(registry, "_tasks", {}),
        "capability": getattr(registry, "_capabilities", {}),
    }
    for kind in kinds:
        if kind == "hook":
            for hook_list in getattr(registry, "_hooks", {}).values():
                for entry in hook_list:
                    yield _entry_row(entry)
            continue
        for entry in sources.get(kind, {}).values():
            yield _entry_row(entry)


def _entry_row(entry: Any) -> tuple[str, Any, str]:
    name = getattr(getattr(entry, "meta", None), "name", getattr(entry, "name", "?"))
    return str(name), entry.source_path, entry.scan_root


def _signed_status(source_path: Any) -> str:
    """"signed" if the source file's ``.arcsig`` verifies, else "unsigned"/"unknown"."""
    try:
        content = source_path.read_bytes()
    except OSError:
        return "unknown"
    return "signed" if verify_file(source_path, content) else "unsigned"


__all__ = ["ExtensionStatus", "inspect_extensions"]
