"""Per-agent runtime state for the thin memory (Brain) wiring.

The memory hooks/tool/background-task share the config-selected
:class:`~arcagent.brain.Brain`, the bus (for ACL gating), and small per-turn
bookkeeping (a once-per-turn recall cache; a capture counter + last-activity
clock that trigger consolidation). Decorator-stamped functions read this lazily
via :func:`state` after :func:`configure` runs once at agent startup, bound to
a :class:`contextvars.ContextVar`.

Task 27/32: a plain module global here is silently overwritten by whichever
agent's ``asyncio.Task`` most recently called ``configure()`` — see
``arcagent/builtins/capabilities/_runtime.py`` for the full rationale. Safe
for ``memory_consolidate_loop`` (the ``@background_task``) too:
``capability_registry.py`` spawns every ``@background_task`` via a plain
``asyncio.create_task()`` call that always fires AFTER ``configure()`` has
already run, in the SAME agent-startup task — asyncio's automatic
context-copy on task creation captures this agent's state into the
consolidation loop's own isolated context for its whole lifetime, with no
``contextvars.copy_context()`` special-casing needed.

Mirrors :mod:`arcagent.modules.memory_acl._runtime`. When the selected brain is a
:class:`~arcagent.brain.NullBrain`, ``active`` is ``False`` and every hook
short-circuits — memory is a truly silent no-op (no events, no files).
"""

from __future__ import annotations

import contextvars
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
    last_consolidate_at: float = field(default_factory=time.monotonic)


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_memory_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    bus: Any = None,
    agent_did: str = "",
    agent_name: str = "",
    identity: Any = None,
    policy_pipeline: Any = None,
) -> None:
    """Bind module state for the CURRENT asyncio task; select the Brain.

    Called once at agent startup. ``identity`` (the agent's signer) and
    ``policy_pipeline`` are threaded to the Brain so the agentic consolidation engine's
    memory-tool writes are signed + policy-authorized.
    """
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
        identity=identity,
        policy_pipeline=policy_pipeline,
        memory_dynamics=dict(cfg.dynamics),
    )
    new_state = _State(
        config=cfg,
        brain=brain,
        workspace=ws,
        telemetry=telemetry,
        bus=bus,
        agent_did=agent_did,
        active=not isinstance(brain, NullBrain),
    )
    _state_var.set(new_state)
    _logger.info("memory module configured (brain=%s, active=%s)", cfg.brain, new_state.active)


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "memory module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return current


def bind(state_obj: _State) -> None:
    """Idempotently bind an already-built ``_State`` into the CURRENT task.

    Cheap — one ``.set()`` call, no construction. Called at the top of
    every turn-dispatch entry point (task 27 follow-up hotfix) so a turn
    running in a fresh sibling ``asyncio.Task`` — not a descendant of the
    task that ran ``configure()`` — still sees this agent's state.
    """
    _state_var.set(state_obj)


def reset() -> None:
    """Test-only: clear runtime state."""
    _state_var.set(None)


__all__ = ["bind", "configure", "reset", "state"]
