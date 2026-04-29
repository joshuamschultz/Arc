"""Per-agent memory module runtime context.

The memory module's hooks, tools, and the entity-extractor background
task share state (workspace path, internal helpers, eval model cache,
per-trace before-snapshots, semaphore, background tasks). Decorator-
stamped functions can't carry that state in a closure, so it lives in
a module-level :class:`_State` instance configured by the agent at
startup.

Mirrors the pattern in :mod:`arcagent.modules.policy._runtime` and
:mod:`arcagent.builtins.capabilities._runtime`. Single-agent-per-
process is the assumption; this is shared mutable state for one agent.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcagent.core.config import EvalConfig
from arcagent.modules.memory.config import MemoryConfig
from arcagent.modules.memory.entity_extractor import EntityExtractor
from arcagent.modules.memory.hybrid_search import HybridSearch
from arcagent.modules.memory.markdown_memory import (
    ContextGuard,
    IdentityAuditor,
    NoteManager,
)

_logger = logging.getLogger("arcagent.modules.memory._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across memory hooks/tools/task."""

    config: MemoryConfig
    eval_config: EvalConfig
    workspace: Path
    telemetry: Any
    llm_config: Any
    notes: NoteManager
    context_guard: ContextGuard
    identity_auditor: IdentityAuditor
    entity_extractor: EntityExtractor
    hybrid_search: HybridSearch
    eval_label: str
    eval_model: Any = None
    # Latest assistant/user pair captured by post_respond — drained by
    # the background entity-extractor task. Treated as a single-slot
    # mailbox: writers overwrite, the task reads-and-clears.
    pending_messages: list[dict[str, Any]] = field(default_factory=list)
    background_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    semaphore: asyncio.Semaphore | None = None
    hook_active: bool = False


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | None = None,
    eval_config: EvalConfig | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    llm_config: Any = None,
    agent_name: str = "",
) -> None:
    """Bind module state. Called once at agent startup."""
    global _state
    cfg = MemoryConfig(**(config or {}))
    ec = eval_config or EvalConfig()
    ws = workspace.resolve()
    _state = _State(
        config=cfg,
        eval_config=ec,
        workspace=ws,
        telemetry=telemetry,
        llm_config=llm_config,
        notes=NoteManager(ws, cfg),
        context_guard=ContextGuard(cfg.context_budget_tokens),
        identity_auditor=IdentityAuditor(ws, telemetry),
        entity_extractor=EntityExtractor(eval_config=ec, workspace=ws, telemetry=telemetry),
        hybrid_search=HybridSearch(ws, cfg),
        eval_label=f"{agent_name}/memory" if agent_name else "memory",
        semaphore=asyncio.Semaphore(ec.max_concurrent),
    )


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
