"""SPEC arcui-reality-mirror COMP-007 — capability inventory seam.

A single read seam over :class:`CapabilityLoader` that enumerates every skill
and capability tool an agent would load across the four scan roots (package
builtins, the global ``~/.arc/capabilities`` root, the per-agent
``<agent>/capabilities`` root, and the agent-authored
``<agent>/workspace/capabilities`` root) and reports each item's loader/TOFU
verdict verbatim.

arcui consumes this instead of globbing skill or tool paths itself
(REQ-093/094/096). No discovery or verification logic lives here: the loader
owns both, and :attr:`CapabilityInventoryItem.status` is whatever the loader
recorded — never re-derived, so a new verdict added to the loader flows through
untouched.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

import arcagent.builtins.capabilities as _builtins_pkg
from arcagent.capabilities.capability_loader import CapabilityLoader, ScanRoot
from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.core.tofu_layer import TofuLayer

# Only skills and capability tools surface in the inventory; hooks, background
# tasks, and capability classes are out of scope for the arcui capability views.
_INVENTORY_KINDS: frozenset[str] = frozenset({"skill", "tool"})
_DEFAULT_GLOBAL_ROOT = Path("~/.arc/capabilities")


class CapabilityInventoryItem(BaseModel):
    """One enumerated capability with its verbatim loader verdict.

    ``status`` mirrors :attr:`CapabilityOutcome.status` exactly — ``"loaded"``,
    a TOFU decision (``"deny"`` / ``"new_sighting"``), ``"unsigned"``,
    ``"invalid"``, ``"error"``, or any future verdict the loader introduces.
    """

    model_config = ConfigDict(frozen=True)

    kind: str
    name: str
    version: str
    description: str
    source_root: str
    status: str
    status_detail: str


def _resolve_scan_roots(
    agent_dir: Path,
    workspace_dir: Path | None,
    global_root: Path | None,
    builtins_root: Path | None,
) -> list[ScanRoot]:
    """Build the four-root scan list, mirroring ``setup_capabilities``.

    Optional roots are included only when they exist on disk, matching the
    loader's own precedence order (builtins, global, agent, workspace).
    """
    builtins = builtins_root if builtins_root is not None else Path(_builtins_pkg.__file__).parent
    roots: list[ScanRoot] = [
        ("builtins", builtins),
        ("builtins-skills", builtins / "skills"),
    ]
    resolved_global = (
        global_root if global_root is not None else _DEFAULT_GLOBAL_ROOT
    ).expanduser()
    if resolved_global.is_dir():
        roots.append(("global", resolved_global))
    agent_caps = agent_dir / "capabilities"
    if agent_caps.is_dir():
        roots.append(("agent", agent_caps))
    workspace = workspace_dir if workspace_dir is not None else agent_dir / "workspace"
    workspace_caps = workspace / "capabilities"
    if workspace_caps.is_dir():
        roots.append(("workspace", workspace_caps))
    return roots


async def collect_capability_inventory(
    agent_dir: Path,
    *,
    workspace_dir: Path | None = None,
    global_root: Path | None = None,
    builtins_root: Path | None = None,
    tofu: TofuLayer | None = None,
    require_signature: bool = False,
    trusted_public_key: bytes | None = None,
    allow_all_imports: bool = False,
    allowed_imports: frozenset[str] = frozenset(),
) -> list[CapabilityInventoryItem]:
    """Enumerate an agent's skills and capability tools with verbatim verdicts.

    Drives :class:`CapabilityLoader` over the four scan roots against a throwaway
    :class:`CapabilityRegistry` (this is a read seam; it never mutates the live
    agent registry). The trust arguments mirror the loader's construction so the
    caller can reproduce the agent's real tier posture — a personal-tier
    :class:`TofuLayer` with a pinned key surfaces signed workspace sources as
    ``loaded`` and unsigned ones as ``deny``.
    """
    scan_roots = _resolve_scan_roots(agent_dir, workspace_dir, global_root, builtins_root)
    loader = CapabilityLoader(
        scan_roots=scan_roots,
        registry=CapabilityRegistry(),
        tofu=tofu,
        require_signature=require_signature,
        trusted_public_key=trusted_public_key,
        allow_all_imports=allow_all_imports,
        allowed_imports=allowed_imports,
    )
    delta = await loader.scan_and_register()
    return [
        CapabilityInventoryItem(
            kind=outcome.kind,
            name=outcome.name,
            version=outcome.version,
            description=outcome.description,
            source_root=outcome.scan_root,
            status=outcome.status,
            status_detail=outcome.status_detail,
        )
        for outcome in delta.outcomes
        if outcome.kind in _INVENTORY_KINDS
    ]


__all__ = ["CapabilityInventoryItem", "collect_capability_inventory"]
