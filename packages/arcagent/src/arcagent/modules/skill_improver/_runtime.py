"""Per-agent skill_improver module runtime context.

The skill_improver module's hooks share state (config, trace collector,
optimization engine, candidate store, evaluator, eval model cache,
background tasks, semaphore). Decorator-stamped functions in
capabilities.py can't carry that state in a closure, so it lives in a
module-level :class:`_State` instance configured by the agent at startup.

Mirrors the pattern in :mod:`arcagent.modules.policy._runtime` and
:mod:`arcagent.modules.memory._runtime`. Single-agent-per-process model.

The ``skill_registry`` field is duck-typed (``Any``) to decouple from
the legacy :class:`arcagent.core.skill_registry.SkillRegistry`. The
agent rewire layer may pass either the legacy registry or a wrapper
around :class:`arcagent.core.capability_registry.CapabilityRegistry`
that exposes the same ``.skills`` list and ``.discover(...)`` no-op
surface. This module never imports from ``arcagent.core.skill_registry``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcagent.core.config import EvalConfig
from arcagent.modules.skill_improver.candidate_store import CandidateStore
from arcagent.modules.skill_improver.config import SkillImproverConfig
from arcagent.modules.skill_improver.guardrails import Guardrails

_logger = logging.getLogger("arcagent.modules.skill_improver._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across skill_improver hooks."""

    config: SkillImproverConfig
    eval_config: EvalConfig
    telemetry: Any
    workspace: Path
    llm_config: Any
    # Duck-typed: legacy SkillRegistry or CapabilityRegistry wrapper.
    # Accessed via .skills (list of SkillMeta) and .discover(ws, ws).
    skill_registry: Any
    guardrails: Guardrails
    candidate_store: CandidateStore
    eval_label: str
    # Lazily initialised in the agent:ready hook once skill_registry arrives.
    trace_collector: Any = None
    eval_model: Any = None
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
    skill_registry: Any = None,
    agent_name: str = "",
) -> None:
    """Bind module state. Called once at agent startup.

    ``skill_registry`` is duck-typed — pass the legacy SkillRegistry or
    any wrapper that exposes ``.skills`` and ``.discover(ws, ws)``.
    ``None`` is acceptable; the agent:ready hook re-checks and initialises
    the TraceCollector once a registry is available.
    """
    global _state
    cfg = SkillImproverConfig(**(config or {}))
    ec = eval_config or EvalConfig()
    ws = workspace.resolve()
    _state = _State(
        config=cfg,
        eval_config=ec,
        telemetry=telemetry,
        workspace=ws,
        llm_config=llm_config,
        skill_registry=skill_registry,
        guardrails=Guardrails(cfg),
        candidate_store=CandidateStore(ws),
        eval_label=f"{agent_name}/skill_improver" if agent_name else "skill_improver",
        semaphore=asyncio.Semaphore(ec.max_concurrent),
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        raise RuntimeError(
            "skill_improver module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


__all__ = ["configure", "reset", "state"]
