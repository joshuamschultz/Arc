"""Planning module configuration (SPEC-040)."""

from __future__ import annotations

from arcagent.core.module_config import ModuleConfig


class PlanningConfig(ModuleConfig):
    """Planning module configuration."""

    enabled: bool = False
    # Bound replan ceiling — never run away (REQ-031).
    max_replans: int = 3
    # Aggregate plan budget, sliced onto per-step run ceilings (REQ-022).
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    # SPEC-043 concurrent Plan-Execute. Default False = interim sequential
    # walk (one ready step at a time). When True the orchestrator dispatches
    # the whole ready DAG frontier concurrently under reserve-then-settle.
    concurrent: bool = False
    # Max branches dispatched at once when ``concurrent`` is set (REQ-056).
    max_parallel: int = 8
