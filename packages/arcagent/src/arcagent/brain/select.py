"""Config-driven Brain selection — the SPEC-047 pluggable-brain seam.

Maps the ``[modules.memory] brain`` setting to a concrete :class:`Brain`:

* ``"none"``       → :class:`NullBrain` (default; memory off, zero files).
* ``"arcmemory"``  → ``arcmemory.ArcMemoryBrain`` (lazy import — arcagent has no
  static dependency on any memory package; missing install degrades to NullBrain
  with a warning rather than crashing the agent).
* ``"auto"``       → ``arcmemory`` if importable, else NullBrain.
* dotted class path → a user-supplied Brain (BYO), instantiated ``cls(workspace, did)``.

arcagent never imports a memory type at module load; the only ``import arcmemory``
is lazy, inside :func:`select_brain`, and guarded.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

from arcagent.brain.protocol import Brain, NullBrain

_logger = logging.getLogger("arcagent.brain.select")


def select_brain(
    setting: str,
    *,
    workspace: Path,
    agent_did: str,
    tier: str = "personal",
    audit_sink: Any = None,
) -> Brain:
    """Return the configured Brain (fail-safe: any error degrades to NullBrain)."""
    choice = (setting or "none").strip()
    if choice in ("none", "", "null"):
        return NullBrain()
    if choice in ("arcmemory", "auto"):
        brain = _try_arcmemory(workspace, agent_did, tier, audit_sink)
        if brain is not None:
            return brain
        if choice == "arcmemory":
            _logger.warning(
                "memory brain='arcmemory' but arcmemory is not installed; "
                "running memory-less (NullBrain)"
            )
        return NullBrain()
    return _load_custom(choice, workspace, agent_did)


def _try_arcmemory(workspace: Path, agent_did: str, tier: str, audit_sink: Any) -> Brain | None:
    """Build ``ArcMemoryBrain`` if arcmemory is importable, else ``None``."""
    try:
        arcmemory = importlib.import_module("arcmemory")
    except ImportError:
        return None
    safe_tier = tier if tier in ("personal", "enterprise", "federal") else "personal"
    config = arcmemory.MemoryConfig.for_tier(safe_tier)
    brain: Brain = arcmemory.ArcMemoryBrain(
        workspace, agent_did, config=config, audit_sink=audit_sink
    )
    return brain


def _load_custom(class_path: str, workspace: Path, agent_did: str) -> Brain:
    """Import + instantiate a BYO Brain from a dotted ``module:Class`` / ``module.Class``."""
    module_name, _, attr = class_path.replace(":", ".").rpartition(".")
    if not module_name:
        raise ValueError(f"invalid brain class path: {class_path!r}")
    cls = getattr(importlib.import_module(module_name), attr)
    brain: Brain = cls(workspace, agent_did)
    return brain


__all__ = ["select_brain"]
