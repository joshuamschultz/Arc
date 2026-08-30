"""``AgentType`` descriptor + folder-scan registry (H-040 §2.1/§2.3).

A harness kind is a folder, exactly as a gateway platform is (SPEC-065): a
directory under ``arcteam/harness/types/<name>/`` exporting a module-level
``AGENT_TYPE = AgentType(...)`` is discovered by a scan; deleting the folder
deletes the type and the core learns neither name. Nothing here is edited to add
one — the property that makes the seam deletable (§11).

**Trust is NOT a descriptor field.** The descriptor carries only ``name`` and
``build``; whether a type is trusted and where it runs is decided in
:mod:`arcteam.harness.trust`, pinned in fleet code, so a foreign folder can never
self-bless (the trap called out in ``agent_fleet.py``'s control-plane docstring).
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from arcteam.harness.protocol import HarnessAdapter

_logger = logging.getLogger("arcteam.harness.agent_type")

#: Import path of the package scanned for harness-type folders.
_TYPES_PACKAGE = "arcteam.harness.types"

#: Module-level name a type folder exports to declare itself.
_DESCRIPTOR = "AGENT_TYPE"

#: Type names: lowercase, start with a letter, max 32 chars — blocks
#: "../evil", path traversal, injection (ASI04). Mirrors the adapter guard.
_VALID_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class AgentType(BaseModel):
    """A discovered harness kind. Folder-scanned exactly like adapters (SPEC-065).

    ``build`` constructs a live member (a :class:`HarnessAdapter`) from its
    :class:`~arcteam.types.Entity` and whatever runtime handles the fleet passes.
    The signature is intentionally loose (``**kwargs``) so a native and a foreign
    type can accept different collaborators without the descriptor knowing.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str
    build: Callable[..., HarnessAdapter]


def validate_type_name(name: str) -> None:
    """Reject harness-type names that could enable path traversal or injection."""
    if not _VALID_NAME_RE.match(name):
        raise ValueError(f"invalid harness type name {name!r}; must match [a-z][a-z0-9_]{{0,31}}")


def discover_agent_types() -> list[AgentType]:
    """Scan ``arcteam/harness/types/`` for folders exporting an ``AGENT_TYPE``.

    Repeatable and uncached: a folder added, fixed, or deleted between two scans
    is reflected in the next. A folder that raises on import is logged and
    skipped so one broken extension never takes the fleet down (ASI04).
    """
    types: list[AgentType] = []
    try:
        package = importlib.import_module(_TYPES_PACKAGE)
    except ImportError:
        return types
    search_paths = [str(p) for p in package.__path__]
    for info in sorted(pkgutil.iter_modules(search_paths), key=lambda i: i.name):
        descriptor = _load_descriptor(info.name)
        if descriptor is not None:
            types.append(descriptor)
    _logger.info(
        "harness: agent types loaded: %s",
        ", ".join(t.name for t in types) or "none",
    )
    return types


def _load_descriptor(name: str) -> AgentType | None:
    """Import one candidate module and return its ``AGENT_TYPE``, or None."""
    if name.startswith("_"):
        return None
    try:
        validate_type_name(name)
    except ValueError:
        _logger.warning("harness: blocked type folder with invalid name %r", name)
        return None
    try:
        module = importlib.import_module(f"{_TYPES_PACKAGE}.{name}")
    except Exception as exc:  # reason: one broken folder must not take the fleet down
        _logger.exception("harness: type folder %r failed to import and skipped: %s", name, exc)
        return None
    descriptor: Any = getattr(module, _DESCRIPTOR, None)
    if descriptor is None:
        return None
    if not isinstance(descriptor, AgentType):
        _logger.warning("harness: %r exports %s, not an AgentType", name, type(descriptor))
        return None
    if descriptor.name != name:
        _logger.warning(
            "harness: folder %r declares itself %r — a type is named by its folder",
            name,
            descriptor.name,
        )
        return None
    return descriptor


def find_agent_type(name: str) -> AgentType | None:
    """Return the discovered :class:`AgentType` named ``name``, or None."""
    for descriptor in discover_agent_types():
        if descriptor.name == name:
            return descriptor
    return None


# Kept for the physical-removal architecture test — the scanned folder root.
TYPES_ROOT = Path(__file__).parent / "types"


__all__ = [
    "TYPES_ROOT",
    "AgentType",
    "discover_agent_types",
    "find_agent_type",
    "validate_type_name",
]
