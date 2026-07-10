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
    import arcagent.builtins.capabilities as builtins_pkg
    from arcagent.capabilities.capability_loader import CapabilityLoader
    from arcagent.capabilities.capability_registry import CapabilityRegistry

    builtins_root = Path(builtins_pkg.__file__).parent
    roots: list[tuple[str, Path]] = [
        ("builtins", builtins_root),
        ("builtins-skills", builtins_root / "skills"),
    ]
    global_root = Path("~/.arc/capabilities").expanduser()
    if global_root.is_dir():
        roots.append(("global", global_root))
    if agent_root is not None:
        for name, sub in (("agent", "capabilities"), ("workspace", "workspace/capabilities")):
            path = agent_root / sub
            if path.is_dir():
                roots.append((name, path))
    roots.extend(_enabled_module_roots(config))

    registry = CapabilityRegistry()
    loader = CapabilityLoader(scan_roots=roots, registry=registry, allow_all_imports=True)
    try:
        asyncio.run(loader.scan_and_register())
    except Exception:  # reason: read-only listing must degrade, not crash
        _logger.warning("could not build capability registry", exc_info=True)
        return None
    return registry


def _enabled_module_roots(config: Any) -> list[tuple[str, Path]]:
    """Per-module ``capabilities.py`` roots for every ENABLED module.

    Mirrors ``agent_lifecycle.setup_capabilities``'s ``modules_dir`` loop
    exactly — the same enablement check, the same "module has capabilities.py"
    gate — so a listing command never diverges from what the agent would
    actually scan at startup.
    """
    import arcagent.modules as modules_pkg

    modules_dir = Path(modules_pkg.__file__).parent
    roots: list[tuple[str, Path]] = []
    for mod_name, mod_entry in getattr(config, "modules", {}).items():
        if not getattr(mod_entry, "enabled", False):
            continue
        mod_dir = modules_dir / mod_name
        if (mod_dir / "capabilities.py").is_file():
            roots.append((f"module:{mod_name}", mod_dir))
    return roots


__all__ = ["build_capability_registry"]
