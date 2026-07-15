"""SPEC-047 — ``select_extension``: the one copy of the select-one logic.

This is the single home of the choice dispatch, the fail-closed BYO allowlist gate,
the generic provider-entrypoint importer, and the dotted-path importer that
``brain/select.py`` and ``skilladapt/select.py`` used to duplicate. Both seams are now
thin :class:`ExtensionPoint` instances that call this.

arcagent never statically imports an extension package or any BYO module — the only import
path is the lazy, guarded one inside :func:`_try_builtin` / :func:`_try_provider` /
:func:`_load_byo`, so ``pip install arcagent`` alone still boots on Null defaults (REQ-005).
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
    (degrade to Null if unimportable — silent for ``auto``, warned otherwise); a ``:``
    class-path → BYO, refused before import unless allowlisted above the personal tier;
    a bare backend name on a provider point → lazy import + well-known entrypoint (degrade
    to Null with a warning if the package or entrypoint is absent).
    """
    choice = (setting or "none").strip()
    if choice in _NULL_CHOICES:
        return point.null_factory()
    if choice in point.builtin_modules:
        return _resolve_builtin(point, choice, context, logger)
    if ":" in choice:
        return _load_byo(point, choice, tier=tier, allowlist=allowlist, context=context)
    if point.provider_entrypoint is not None:
        return _try_provider(point, choice, context, logger)
    return _load_byo(point, choice, tier=tier, allowlist=allowlist, context=context)


def _resolve_builtin(
    point: ExtensionPoint, choice: str, context: dict[str, Any], logger: Logger
) -> Any:
    """Build a builtin choice, degrading to Null (silent for ``auto``) when unavailable."""
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


def _try_provider(
    point: ExtensionPoint, module_name: str, context: dict[str, Any], logger: Logger
) -> Any:
    """Lazily import a backend module and build via its well-known entrypoint.

    Unlike a BYO class-path this is NOT allowlist-gated: the operator selected an
    installed package by name (its supply-chain trust rides the signed-dependency
    pipeline), mirroring the ungated posture builtins had. A missing package or a package
    without the entrypoint degrades to the Null default with a warning — never a crash.
    """
    entrypoint = point.provider_entrypoint
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        logger.warning(
            "%s=%r selected but its package is not installed; degrading to the Null default",
            point.name,
            module_name,
        )
        return point.null_factory()
    factory = getattr(module, entrypoint or "", None)
    if factory is None:
        logger.warning(
            "%s provider %r exposes no %r entrypoint; degrading to the Null default",
            point.name,
            module_name,
            entrypoint,
        )
        return point.null_factory()
    return factory(context)


def _try_builtin(point: ExtensionPoint, module_name: str, context: dict[str, Any]) -> Any | None:
    """Lazily import a builtin module and build its instance, or ``None`` to degrade."""
    if point.builtin_builder is None:
        return None
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
