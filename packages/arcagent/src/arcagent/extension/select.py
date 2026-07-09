"""SPEC-047 — ``select_extension``: the one copy of the select-one logic.

This is the single home of the choice dispatch, the fail-closed BYO allowlist gate,
and the dotted-path importer that ``brain/select.py`` and ``skilladapt/select.py`` used
to duplicate. Both seams are now thin :class:`ExtensionPoint` instances that call this.

arcagent never statically imports a builtin extension package (arcmemory, arcskill) or
any BYO module — the only import path is the lazy, guarded one inside :func:`_try_builtin`
/ :func:`_load_byo`, so ``pip install arcagent`` alone still boots on Null defaults (REQ-005).
"""

from __future__ import annotations

import importlib
from logging import Logger
from typing import Any

from arcagent.extension.point import ExtensionPoint

_NULL_CHOICES = frozenset({"none", "", "null"})


def select_extension(
    point: ExtensionPoint,
    setting: str,
    *,
    tier: str,
    allowlist: tuple[str, ...],
    context: dict[str, Any],
    logger: Logger,
) -> Any:
    """Resolve a select-one setting to a concrete instance (fail-safe → Null default).

    Dispatch: ``none``/``""``/``null`` → Null; a builtin choice → lazy import + build
    (degrade to Null if unimportable — silent for ``auto``, warned otherwise); a dotted
    class-path → BYO, refused before import unless allowlisted above the personal tier.
    """
    choice = (setting or "none").strip()
    if choice in _NULL_CHOICES:
        return point.null_factory()
    if choice in point.builtin_modules:
        module_name = point.builtin_modules[choice]
        instance = _try_builtin(point, module_name, context)
        if instance is not None:
            return instance
        if choice != "auto":
            logger.warning(
                "%s=%r selected but %r is unavailable; degrading to the Null default",
                point.name,
                choice,
                module_name,
            )
        return point.null_factory()
    return _load_byo(point, choice, tier=tier, allowlist=allowlist, context=context)


def _try_builtin(point: ExtensionPoint, module_name: str, context: dict[str, Any]) -> Any | None:
    """Lazily import a builtin module and build its instance, or ``None`` to degrade."""
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    return point.builtin_builder(module, context)


def _load_byo(
    point: ExtensionPoint,
    class_path: str,
    *,
    tier: str,
    allowlist: tuple[str, ...],
    context: dict[str, Any],
) -> Any:
    """Import + construct a BYO implementation from a dotted ``module:Class`` path.

    Above the personal tier the class-path must be on the operator allowlist (the
    signed/vetted registry posture of SPEC-033) — otherwise it is REFUSED before any
    import, because importing an unverified dotted path is arbitrary code execution at
    startup (ASI04). This gate is the security-critical invariant shared by every seam.
    """
    if tier != "personal" and class_path not in allowlist:
        raise ValueError(
            f"BYO {point.name} class-path {class_path!r} is not on the operator allowlist; "
            f"refusing to import an unverified class-path at tier {tier!r} (fail-closed)"
        )
    module_name, _, attr = class_path.replace(":", ".").rpartition(".")
    if not module_name:
        raise ValueError(f"invalid {point.name} class path: {class_path!r}")
    cls = getattr(importlib.import_module(module_name), attr)
    return point.byo_constructor(cls, context)


__all__ = ["select_extension"]
