"""Per-agent runtime state for the thin memory (Brain) wiring.

The memory hooks/tool/background-task share the config-selected
:class:`~arcagent.brain.Brain`, the bus (for ACL gating), and small per-turn
bookkeeping (a once-per-turn recall cache; a capture counter + last-activity
clock that trigger consolidation). Decorator-stamped functions read this lazily
via :func:`state` after :func:`configure` runs once at agent startup.

Mirrors :mod:`arcagent.modules.memory_acl._runtime`. When the selected brain is a
:class:`~arcagent.brain.NullBrain`, ``active`` is ``False`` and every hook
short-circuits — memory is a truly silent no-op (no events, no files).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcagent.brain import Brain, NullBrain, select_brain
from arcagent.modules.memory.config import MemoryConfig

_logger = logging.getLogger("arcagent.modules.memory._runtime")

_RECALL_CACHE_CAP = 8


@dataclass
class _State:
    """Mutable runtime state shared across the memory hooks/tool/task."""

    config: MemoryConfig
    brain: Brain
    workspace: Path
    telemetry: Any
    bus: Any
    agent_did: str
    active: bool
    # Once-per-turn recall cache: query-hash -> injectable text (bounds the
    # spawn double-assembly to a single retrieve).
    recall_cache: dict[int, str] = field(default_factory=dict)
    # Consolidation trigger bookkeeping.
    events_since_consolidate: int = 0
    last_activity: float = field(default_factory=time.monotonic)


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    bus: Any = None,
    agent_did: str = "",
    agent_name: str = "",
) -> None:
    """Bind module state; select the Brain. Called once at agent startup."""
    global _state
    del agent_name  # accepted for signature-dispatch parity; unused here
    cfg = MemoryConfig(**(config or {}))
    ws = Path(workspace).resolve()
    brain = select_brain(
        cfg.brain,
        workspace=ws,
        agent_did=agent_did,
        tier=cfg.tier,
        embed_backend=cfg.embed_backend,
        embed_model=cfg.embed_model,
        distill_provider=cfg.distill_provider,
        distill_model=cfg.distill_model,
        brain_allowlist=tuple(cfg.brain_allowlist),
    )
    _state = _State(
        config=cfg,
        brain=brain,
        workspace=ws,
        telemetry=telemetry,
        bus=bus,
        agent_did=agent_did,
        active=not isinstance(brain, NullBrain),
    )
    _logger.info("memory module configured (brain=%s, active=%s)", cfg.brain, _state.active)


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        raise RuntimeError(
            "memory module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


__all__ = ["configure", "reset", "state"]
