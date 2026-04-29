"""Per-agent bio_memory module runtime context.

The bio_memory module's hooks and tools share state (workspace, config,
eval model cache, message accumulator, turn counter, background tasks,
semaphore). Decorator-stamped functions can't carry that state in a
closure, so it lives in a module-level :class:`_State` instance
configured by the agent at startup.

Mirrors the pattern in :mod:`arcagent.modules.memory._runtime` and
:mod:`arcagent.modules.policy._runtime`. Single-agent-per-process is
the assumption; this is shared mutable state for one agent.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcagent.core.config import EvalConfig
from arcagent.modules.bio_memory.config import BioMemoryConfig
from arcagent.modules.bio_memory.consolidator import Consolidator
from arcagent.modules.bio_memory.daily_notes import DailyNotes
from arcagent.modules.bio_memory.retriever import Retriever
from arcagent.modules.bio_memory.working_memory import WorkingMemory

_logger = logging.getLogger("arcagent.modules.bio_memory._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across bio_memory hooks and tools."""

    config: BioMemoryConfig
    eval_config: EvalConfig
    workspace: Path
    memory_dir: Path
    telemetry: Any
    llm_config: Any
    agent_id: str
    eval_label: str
    working: WorkingMemory
    daily_notes: DailyNotes
    retriever: Retriever
    consolidator: Consolidator
    eval_model: Any = None
    # Accumulated messages since last consolidation flush
    messages: list[dict[str, Any]] = field(default_factory=list)
    turn_count: int = 0
    background_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    semaphore: asyncio.Semaphore | None = None


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | None = None,
    eval_config: EvalConfig | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    llm_config: Any = None,
    agent_name: str = "",
    team_config: dict[str, Any] | None = None,
) -> None:
    """Bind module state. Called once at agent startup."""
    global _state
    cfg = BioMemoryConfig(**(config or {}))
    ec = eval_config or EvalConfig()
    ws = workspace.resolve()
    memory_dir = ws / "memory"
    # Infer agent_id from workspace parent directory name (matches BioMemoryModule)
    agent_id = ws.parent.name if ws != Path(".").resolve() else ""

    working = WorkingMemory(memory_dir, cfg)
    daily_notes_obj = DailyNotes(memory_dir, cfg)
    retriever = Retriever(
        memory_dir,
        cfg,
        workspace=ws,
        team_entities_dir=_resolve_team_entities_dir(team_config, ws),
    )
    consolidator = Consolidator(
        memory_dir,
        cfg,
        working,
        daily_notes_obj,
        telemetry,
        workspace=ws,
        team_service_factory=_make_team_service_factory(team_config, ws),
        agent_id=agent_id,
        call_timeout=float(ec.timeout_seconds),
    )

    _state = _State(
        config=cfg,
        eval_config=ec,
        workspace=ws,
        memory_dir=memory_dir,
        telemetry=telemetry,
        llm_config=llm_config,
        agent_id=agent_id,
        eval_label=f"{agent_name}/memory" if agent_name else "memory",
        working=working,
        daily_notes=daily_notes_obj,
        retriever=retriever,
        consolidator=consolidator,
        semaphore=asyncio.Semaphore(ec.max_concurrent),
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        raise RuntimeError(
            "bio_memory module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


# --- Private helpers -------------------------------------------------------


def _resolve_team_entities_dir(
    team_config: dict[str, Any] | None,
    workspace: Path,
) -> Path | None:
    """Resolve team entities path from team_config if available."""
    if team_config is None:
        return None
    root = team_config.get("root_path") or team_config.get("root")
    if not root:
        return None
    team_root = Path(root)
    if not team_root.is_absolute():
        team_root = (workspace.parent / team_root).resolve()
    team_entities = team_root / "entities"
    return team_entities if team_entities.exists() else None


def _make_team_service_factory(
    team_config: dict[str, Any] | None,
    workspace: Path,
) -> Any:
    """Build a lazy team-service factory closure (mirrors BioMemoryModule)."""
    if team_config is None:
        return None

    _cache: list[Any] = [None]  # one-element list used as mutable cell

    def _factory() -> Any:
        if _cache[0] is not None:
            return _cache[0]
        try:
            from arcteam.memory.config import TeamMemoryConfig
            from arcteam.memory.service import TeamMemoryService

            root_str = team_config.get("root_path") or team_config.get("root", "")
            if not root_str:
                team_root = Path.home() / ".arc" / "team"
            else:
                team_root = Path(root_str)
                if not team_root.is_absolute():
                    team_root = (workspace.parent / team_root).resolve()

            team_cfg = TeamMemoryConfig(root=team_root)
            _cache[0] = TeamMemoryService(team_cfg)
            return _cache[0]
        except ImportError:
            _logger.debug("arcteam not installed, team memory disabled")
            return None
        except Exception:
            _logger.debug("arcteam init failed, team memory disabled", exc_info=True)
            return None

    return _factory


__all__ = ["configure", "reset", "state"]
