"""Planning module — a real Plan-Execute planner (SPEC-040).

Given a goal, the planner produces a durable, dependency-aware DAG plan
(:mod:`.models`), decomposes it through arcllm (:mod:`.decomposer`), persists
and audits every transition (:mod:`.store`), executes each step as one bounded,
policy- and budget-gated arcrun run through the ``StepExecutor`` seam
(:mod:`.executor`), walks the DAG and replans on failure — bounded so it can
never run away (:mod:`.orchestrator`). The LLM surface + hooks live in
:mod:`.capabilities`; per-agent wiring in :mod:`._runtime`.

Concern boundary: arcagent owns the PLAN, arcrun owns EXECUTION, arcllm owns
INFERENCE, existing infra owns persistence + audit. Zero ``arcagent/core`` LOC.
"""

from __future__ import annotations

from arcagent.modules.planning._runtime import PlanningConfig

__all__ = ["PlanningConfig"]
