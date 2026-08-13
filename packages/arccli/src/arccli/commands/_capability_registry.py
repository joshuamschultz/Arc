"""Shared read-only CapabilityRegistry builder (SPEC-021/047).

Both ``arc ext inspect`` and ``arc agent tools`` need to answer "what would
this agent's REAL tool registry contain at startup?" without booting a live
``ArcAgent`` — ``ArcAgent.startup()`` emits ``agent:ready``, which makes
enabled modules (telegram/slack/scheduler/messaging) actually connect to
network services. That's the wrong side effect for a read-only listing
command, so this mirrors ``arcagent.core.agent_lifecycle.setup_capabilities``'s
scan-root precedence with a standalone ``CapabilityLoader`` instead: builtins
-> builtins-skills -> global -> agent -> workspace -> per-ENABLED-module
``capabilities.py``.

Before this module existed, ``arc ext.py`` had its own partial copy of this
scan (missing the per-module roots) and ``arc agent tools`` had no copy at
all — it only ever looked at the agent's own ``capabilities/`` directory
(task #29). One builder, reused by both, closes both gaps at once.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

_logger = logging.getLogger("arccli.commands.capability_registry")


def build_capability_registry(config: Any, agent_root: Path | None) -> Any | None:
    """Scan every SPEC-021 root the live agent would scan; return a populated registry.

    ``config`` is the agent's loaded ``ArcAgentConfig`` (used only to resolve
    which modules are enabled). ``agent_root`` is the agent directory (used to
    resolve the agent/workspace scan roots); pass ``None`` for a user-wide
    (non-agent-scoped) inspection.

    Best-effort: returns ``None`` if the scan fails outright (e.g. the loader
    itself raises) rather than crashing a read-only listing command.
    """
    import arcagent

    builtins_root = arcagent.builtin_capabilities_path()
    roots: list[tuple[str, Path]] = [
        ("builtins", builtins_root),
        ("builtins-skills", builtins_root / "skills"),
    ]
    global_root = arcagent.global_capabilities_root()
    if global_root.is_dir():
        roots.append(("global", global_root))
    if agent_root is not None:
        for name, sub in (("agent", "capabilities"), ("workspace", "workspace/capabilities")):
            path = agent_root / sub
            if path.is_dir():
                roots.append((name, path))
    roots.extend(_enabled_module_roots(config, agent_root))

    registry = arcagent.CapabilityRegistry()
    loader = arcagent.CapabilityLoader(
        scan_roots=roots,
        registry=registry,
        # Read-only enumeration: import policy must never hide a discoverable
        # tool from the listing, so scan under the allow-all (personal) policy
        # regardless of the agent's real tier. Signed/gated status is reported
        # separately by the inspect layer.
        import_policy=arcagent.resolve_workspace_import_policy(
            "personal", allow_all_imports=True, allow_imports=[]
        ),
        # Task #39: this is a read-only scan over a throwaway registry — a
        # discovered @background_task (e.g. the memory module's
        # consolidation loop) must never actually start. Its body depends
        # on a live agent's module _runtime being configured, which this
        # CLI listing command never does — the live incident: "arc agent
        # tools" dumped "memory module called before runtime is
        # configured" the instant the scan registered the task.
        spawn_background_tasks=False,
    )
    try:
        asyncio.run(loader.scan_and_register())
    except Exception:  # reason: read-only listing must degrade, not crash
        _logger.warning("could not build capability registry", exc_info=True)
        return None
    return registry


def _enabled_module_roots(config: Any, agent_root: Path | None) -> list[tuple[str, Path]]:
    """Per-module capability roots for every ENABLED module, from the agent's copy.

    Built by the same helper ``agent_lifecycle.setup_capabilities`` calls, so a
    listing command cannot diverge from what the agent scans at startup — the
    divergence this function previously carried, pointing at the source catalog
    while the agent loaded from somewhere else entirely.

    A module's capability surface is copied PER AGENT (REQ-337), so a user-wide
    inspection with no agent in scope has no module capabilities to list.
    """
    import arcagent

    if agent_root is None:
        return []
    enabled = sorted(
        name
        for name, entry in getattr(config, "modules", {}).items()
        if getattr(entry, "enabled", False)
    )
    roots: list[tuple[str, Path]] = []
    arcagent.append_module_scan_roots(roots, agent_root, enabled)
    return roots


__all__ = ["build_capability_registry"]
