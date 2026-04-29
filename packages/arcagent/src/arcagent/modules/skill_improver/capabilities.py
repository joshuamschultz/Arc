"""Decorator-form skill_improver module — SPEC-021 capability surface.

Four ``@hook`` functions mirror :class:`SkillImproverModule`'s
``startup`` bus registrations:

  * ``agent:post_tool``    (priority 200) — detect skill reads, capture
    tool calls within active trace spans.
  * ``agent:post_plan``    (priority 200) — close trace spans at turn end
    and increment the turn counter.
  * ``agent:post_respond`` (priority 150) — check usage thresholds; spawn
    background optimization for skills that have hit the use limit.
  * ``agent:ready``        (priority 100) — extract the skill_registry
    from the event payload, initialise the TraceCollector, and re-index
    skills if a registry was also supplied at configure-time.

No ``@tool`` decorators here — ``skill_versions`` and ``skill_rollback``
are registered by :class:`SkillImproverModule` via the legacy tool
registry path and are preserved unchanged in that class.

State is shared via :mod:`arcagent.modules.skill_improver._runtime`; the
agent configures it once at startup and the capabilities read it lazily.

Duck-typed skill registry access points (exercised at runtime):
  - ``skill_registry.skills``           — list of SkillMeta in agent:ready
  - ``skill_registry.discover(ws, ws)`` — rescan after optimization in
    ``_optimize_skill`` (called from background tasks spawned here)
"""

from __future__ import annotations

import logging
from typing import Any

from arcagent.modules.skill_improver import _runtime
from arcagent.tools._decorator import hook
from arcagent.utils.model_helpers import get_eval_model, spawn_background

_logger = logging.getLogger("arcagent.modules.skill_improver.capabilities")


# ---------------------------------------------------------------------------
# Lazy eval-model helper
# ---------------------------------------------------------------------------


def _get_eval_model() -> Any:
    """Lazy-init eval model with cache via ``_runtime.state``."""
    st = _runtime.state()
    result = get_eval_model(
        cached_model=st.eval_model,
        eval_config=st.eval_config,
        llm_config=st.llm_config,
        logger=_logger,
        agent_label=st.eval_label,
    )
    if result is not None:
        st.eval_model = result
    return result


# ---------------------------------------------------------------------------
# Optimization helper — executed inside background tasks
# ---------------------------------------------------------------------------


async def _optimize_skill(skill_name: str) -> None:
    """Run the full optimization loop for a single skill.

    Mirrors :meth:`SkillImproverModule._optimize_skill`. Called via
    ``spawn_background`` so failures are caught, audited, and never
    propagate to the agent loop.

    Duck-typed registry access:
      - ``st.skill_registry.skills``           — path lookup (list of SkillMeta)
      - ``st.skill_registry.discover(ws, ws)`` — rescan after write
    """
    # Deferred imports keep module-level load fast and avoid circular deps.
    from arcagent.modules.skill_improver.engine import SkillOptimizer
    from arcagent.modules.skill_improver.evaluator import SkillEvaluator
    from arcagent.modules.skill_improver.reflector import SkillReflector

    st = _runtime.state()
    if st.trace_collector is None:
        return

    current_turn = st.trace_collector.turn_number
    all_traces = st.trace_collector.load_traces(skill_name)
    traces = [
        t for t in all_traces if current_turn - t.turn_number >= st.config.trace_buffer_turns
    ]

    # Extract skill tags for exempt-tag guardrail check (R34).
    skill_tags = _get_skill_tags(skill_name)
    if not st.guardrails.check_eligible(
        skill_name,
        traces,
        current_turn=current_turn,
        skill_tags=skill_tags,
    ):
        _logger.debug("Skill %s not eligible for optimization", skill_name)
        return

    skill_path = _get_skill_path(skill_name)
    if skill_path is None:
        return
    try:
        current_text = skill_path.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        _logger.warning("Skill file not found: %s", skill_path)
        return

    model = _get_eval_model()
    if model is None:
        _logger.warning("No eval model available, skipping optimization for %s", skill_name)
        return

    if st.telemetry:
        st.telemetry.audit_event(
            "skill_improver.optimization_started",
            {
                "skill_name": skill_name,
                "trace_count": len(traces),
                "current_generation": st.guardrails.get_generation(skill_name),
            },
        )

    evaluator = SkillEvaluator(st.config, llm=model)
    reflector = SkillReflector(st.config, llm=model)
    optimizer = SkillOptimizer(
        config=st.config,
        evaluator=evaluator,
        reflector=reflector,
        guardrails=st.guardrails,
        store=st.candidate_store,
    )

    result = await optimizer.optimize(skill_name, current_text, traces)
    if result is None:
        return

    if result.best_candidate.id == "seed":
        _logger.info("No improvement found for %s", skill_name)
        return

    optimizer.apply_result(
        skill_name,
        result.best_candidate,
        skill_path=skill_path,
        seed_scores=result.seed_scores,
        trace_ids=[t.trace_id for t in traces],
    )

    st.guardrails.set_generation(skill_name, result.best_candidate.generation)

    # Re-index skills via duck-typed registry (exercises .discover and .skills).
    if st.skill_registry is not None:
        st.skill_registry.discover(st.workspace, st.workspace)
        st.trace_collector.index_skills(st.skill_registry)

    if st.telemetry:
        st.telemetry.audit_event(
            "skill_improver.optimization_completed",
            result.to_dict(),
        )


def _get_skill_path(skill_name: str) -> Any:
    """Return the Path for a skill from the duck-typed registry.

    Exercises ``st.skill_registry.skills`` — the list of SkillMeta
    objects each exposing ``.name`` and ``.file_path``.
    """
    st = _runtime.state()
    if st.skill_registry is None:
        return None
    for skill in st.skill_registry.skills:  # duck-typed: .skills list
        if skill.name == skill_name:
            return skill.file_path
    return None


def _get_skill_tags(skill_name: str) -> list[str]:
    """Read frontmatter tags from skill file for exempt-tag check (R34)."""
    from arcagent.utils.sanitizer import read_frontmatter

    skill_path = _get_skill_path(skill_name)
    if skill_path is None:
        return []
    fm = read_frontmatter(skill_path)
    if fm is None:
        return []
    tags = fm.get("tags", [])
    return list(tags) if isinstance(tags, list) else []


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


@hook(event="agent:post_tool", priority=200)
async def skill_improver_post_tool(ctx: Any) -> None:
    """Detect skill reads and capture tool calls within active trace spans.

    Delegates entirely to the TraceCollector once it is initialised.
    No-ops silently if the collector is not yet ready (agent:ready
    hasn't fired or no skill_registry was available).
    """
    st = _runtime.state()
    if st.trace_collector is not None:
        await st.trace_collector.on_post_tool(ctx)


@hook(event="agent:post_plan", priority=200)
async def skill_improver_post_plan(ctx: Any) -> None:
    """Close the active trace span and increment the turn counter at turn end."""
    st = _runtime.state()
    if st.trace_collector is not None:
        await st.trace_collector.on_post_plan(ctx)


@hook(event="agent:post_respond", priority=150)
async def skill_improver_post_respond(ctx: Any) -> None:
    """Check per-skill usage thresholds; spawn background optimization when met.

    Resets the usage count before spawning so a slow background run
    does not double-trigger for the same batch of traces.
    """
    st = _runtime.state()
    if st.trace_collector is None:
        return

    semaphore = st.semaphore
    if semaphore is None:
        raise RuntimeError("skill_improver module semaphore not configured")

    for skill_name, count in st.trace_collector.usage_counts.items():
        if count >= st.config.optimize_after_uses:
            st.trace_collector.reset_count(skill_name)
            spawn_background(
                _optimize_skill(skill_name),
                background_tasks=st.background_tasks,
                semaphore=semaphore,
                eval_config=st.eval_config,
                telemetry=st.telemetry,
                audit_event_name="skill_improver.optimization_error",
                logger=_logger,
            )


@hook(event="agent:ready", priority=100)
async def skill_improver_ready(ctx: Any) -> None:
    """Initialise TraceCollector from the skill_registry in the ready payload.

    The agent:ready event carries a ``skill_registry`` in ``ctx.data``.
    If present it takes precedence over the registry supplied at
    configure-time (supports hot-wire during the legacy → CapabilityRegistry
    transition). If neither is available, trace collection is disabled for
    this session with a warning.

    Duck-typed registry access: ``skill_registry.skills`` is exercised
    inside TraceCollector.__init__ → ``index_skills``.
    """
    from arcagent.modules.skill_improver.trace_collector import TraceCollector

    st = _runtime.state()

    # Prefer the registry delivered via the event payload; fall back to
    # the one supplied at configure() time.
    registry = ctx.data.get("skill_registry") or st.skill_registry
    if registry is None:
        _logger.warning("No skill_registry in agent:ready event, trace collection disabled")
        return

    st.skill_registry = registry
    st.trace_collector = TraceCollector(
        skill_registry=registry,  # duck-typed: uses .skills inside index_skills
        workspace=st.workspace,
        config=st.config,
    )


__all__ = [
    "skill_improver_post_plan",
    "skill_improver_post_respond",
    "skill_improver_post_tool",
    "skill_improver_ready",
]
