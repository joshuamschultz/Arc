"""SkillImproverModule — facade implementing the Module protocol.

Evolutionary optimization of skill procedure documents using
execution traces and LLM-as-judge evaluation. Disabled by default;
requires explicit ``[modules.skill_improver]`` config to activate.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arcagent.core.config import EvalConfig
from arcagent.core.module_bus import EventContext, ModuleContext
from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.modules.skill_improver.candidate_store import CandidateStore
from arcagent.modules.skill_improver.config import SkillImproverConfig
from arcagent.modules.skill_improver.engine import SkillOptimizer
from arcagent.modules.skill_improver.evaluator import SkillEvaluator
from arcagent.modules.skill_improver.guardrails import Guardrails
from arcagent.modules.skill_improver.models import Candidate, MutationEvent
from arcagent.modules.skill_improver.reflector import SkillReflector
from arcagent.utils.model_helpers import get_eval_model, spawn_background
from arcagent.utils.sanitizer import read_frontmatter

_logger = logging.getLogger("arcagent.modules.skill_improver")


class SkillImproverModule:
    """Evolutionary skill optimization module.

    Collects execution traces when skills are used, then periodically
    runs an optimization loop to improve skill body text via constrained
    mutations, LLM-as-judge evaluation, and Pareto frontier selection.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        eval_config: EvalConfig | None = None,
        telemetry: Any = None,
        workspace: Path = Path("."),
        llm_config: Any | None = None,
    ) -> None:
        self._config = SkillImproverConfig(**(config or {}))
        self._eval_config = eval_config or EvalConfig()
        self._llm_config = llm_config
        self._telemetry = telemetry
        self._workspace = workspace.resolve() if workspace != Path(".") else workspace

        self._background_tasks: set[asyncio.Task[None]] = set()
        self._semaphore = asyncio.Semaphore(self._eval_config.max_concurrent)

        # Internal components — initialized lazily on ready
        self._collector: Any = None
        self._guardrails = Guardrails(self._config)
        self._store = CandidateStore(self._workspace)
        self._eval_model: Any = None
        self._skill_registry: Any = None

    @property
    def name(self) -> str:
        return "skill_improver"

    async def startup(self, ctx: ModuleContext) -> None:
        """Register bus handlers and tools."""
        bus = ctx.bus
        bus.subscribe("agent:post_tool", self._on_post_tool, priority=200)
        bus.subscribe("agent:post_plan", self._on_post_plan, priority=200)
        bus.subscribe("agent:post_respond", self._on_post_respond, priority=150)
        bus.subscribe("agent:ready", self._on_ready, priority=100)

        self._register_tools(ctx.tool_registry)

    async def shutdown(self) -> None:
        """Await in-flight background tasks before teardown."""
        if self._background_tasks:
            _logger.info(
                "Awaiting %d background task(s) before shutdown",
                len(self._background_tasks),
            )
            await asyncio.gather(
                *self._background_tasks,
                return_exceptions=True,
            )

    # -- Bus handlers --

    async def _on_post_tool(self, ctx: EventContext) -> None:
        """Detect skill reads and capture tool calls within active spans."""
        if self._collector is not None:
            await self._collector.on_post_tool(ctx)

    async def _on_post_plan(self, ctx: EventContext) -> None:
        """Close trace spans at turn end."""
        if self._collector is not None:
            await self._collector.on_post_plan(ctx)

    async def _on_post_respond(self, ctx: EventContext) -> None:
        """Check usage threshold and trigger optimization."""
        if self._collector is None:
            return
        for skill_name, count in self._collector.usage_counts.items():
            if count >= self._config.optimize_after_uses:
                self._collector.reset_count(skill_name)
                spawn_background(
                    self._optimize_skill(skill_name),
                    background_tasks=self._background_tasks,
                    semaphore=self._semaphore,
                    eval_config=self._eval_config,
                    telemetry=self._telemetry,
                    audit_event_name="skill_improver.optimization_error",
                    logger=_logger,
                )

    async def _on_ready(self, ctx: EventContext) -> None:
        """Index skill paths from registry."""
        from arcagent.modules.skill_improver.trace_collector import TraceCollector

        skill_registry = ctx.data.get("skill_registry")
        if skill_registry is None:
            _logger.warning("No skill_registry in ready event, trace collection disabled")
            return
        self._skill_registry = skill_registry
        self._collector = TraceCollector(
            skill_registry=skill_registry,
            workspace=self._workspace,
            config=self._config,
        )

    # -- Optimization --

    async def _optimize_skill(self, skill_name: str) -> None:
        """Run optimization for a single skill."""
        if self._collector is None:
            return

        # R32: Filter traces by turn age (temporal buffer breaks feedback loops)
        current_turn = self._collector.turn_number
        all_traces = self._collector.load_traces(skill_name)
        traces = [
            t
            for t in all_traces
            if current_turn - t.turn_number >= self._config.trace_buffer_turns
        ]

        # R34: Extract skill tags for exempt-tag check
        skill_tags = self._get_skill_tags(skill_name)
        if not self._guardrails.check_eligible(
            skill_name,
            traces,
            current_turn=current_turn,
            skill_tags=skill_tags,
        ):
            _logger.debug("Skill %s not eligible for optimization", skill_name)
            return

        # Get skill file path from registry
        skill_path = self._get_skill_path(skill_name)
        if skill_path is None:
            return
        try:
            current_text = skill_path.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError):
            _logger.warning("Skill file not found: %s", skill_path)
            return

        model = self._get_eval_model()
        if model is None:
            _logger.warning("No eval model available, skipping optimization")
            return

        # M-6: Emit optimization_started audit event
        if self._telemetry:
            self._telemetry.audit_event(
                "skill_improver.optimization_started",
                {
                    "skill_name": skill_name,
                    "trace_count": len(traces),
                    "current_generation": self._guardrails.get_generation(skill_name),
                },
            )

        evaluator = SkillEvaluator(self._config, llm=model)
        reflector = SkillReflector(self._config, llm=model)
        optimizer = SkillOptimizer(
            config=self._config,
            evaluator=evaluator,
            reflector=reflector,
            guardrails=self._guardrails,
            store=self._store,
        )

        result = await optimizer.optimize(skill_name, current_text, traces)
        if result is None:
            return

        # Only apply if improvement detected
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

        # R30: Update generation counter after successful optimization
        self._guardrails.set_generation(skill_name, result.best_candidate.generation)

        # Rescan skill registry
        if self._skill_registry is not None:
            self._skill_registry.discover(self._workspace, self._workspace)
            self._collector.index_skills(self._skill_registry)

        if self._telemetry:
            self._telemetry.audit_event(
                "skill_improver.optimization_completed",
                result.to_dict(),
            )

    def _get_skill_path(self, skill_name: str) -> Path | None:
        """Get the file path for a skill from the registry."""
        if self._skill_registry is None:
            return None
        for skill in self._skill_registry.skills:
            if skill.name == skill_name:
                return skill.file_path
        return None

    def _get_skill_tags(self, skill_name: str) -> list[str]:
        """Extract frontmatter tags from a skill file for exempt-tag checking (R34)."""
        skill_path = self._get_skill_path(skill_name)
        if skill_path is None:
            return []
        fm = read_frontmatter(skill_path)
        if fm is None:
            return []
        tags = fm.get("tags", [])
        return list(tags) if isinstance(tags, list) else []

    def _get_eval_model(self) -> Any:
        """Lazy-init eval model, caching result."""
        result = get_eval_model(
            cached_model=self._eval_model,
            eval_config=self._eval_config,
            llm_config=self._llm_config,
            logger=_logger,
        )
        if result is not None:
            self._eval_model = result
        return result

    # -- Tool registration --

    def _register_tools(self, tool_registry: Any) -> None:
        """Register skill_versions and skill_rollback tools."""
        tool_registry.register(
            RegisteredTool(
                name="skill_versions",
                description="List optimization history for a skill.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "skill_name": {
                            "type": "string",
                            "description": "Name of the skill to query",
                            "maxLength": 200,
                        },
                    },
                    "required": ["skill_name"],
                    "additionalProperties": False,
                },
                transport=ToolTransport.NATIVE,
                execute=self._handle_skill_versions,
            )
        )

        tool_registry.register(
            RegisteredTool(
                name="skill_rollback",
                description="Revert a skill to a previous version (triggers cooloff).",
                input_schema={
                    "type": "object",
                    "properties": {
                        "skill_name": {
                            "type": "string",
                            "description": "Name of the skill to rollback",
                            "maxLength": 200,
                        },
                        "candidate_id": {
                            "type": "string",
                            "description": "ID of the candidate version to revert to",
                            "maxLength": 100,
                        },
                    },
                    "required": ["skill_name", "candidate_id"],
                    "additionalProperties": False,
                },
                transport=ToolTransport.NATIVE,
                execute=self._handle_skill_rollback,
            )
        )

    # -- Tool handlers --

    async def _handle_skill_versions(self, skill_name: str) -> str:
        """List version history for a skill."""
        manifest = self._store.load_manifest(skill_name)
        if not manifest.get("candidates"):
            return f"No optimization history found for '{skill_name}'."
        candidates = manifest["candidates"]
        active_id = manifest.get("active_candidate_id", "")
        lines = [f"Skill: {skill_name}", f"Active: {active_id}", ""]
        for cid, meta in candidates.items():
            marker = " (active)" if cid == active_id else ""
            lines.append(
                f"  {cid}{marker}: gen={meta.get('generation', 0)}, "
                f"parent={meta.get('parent_id', 'none')}, "
                f"scores={json.dumps(meta.get('scores', {}))}"
            )
        return "\n".join(lines)

    async def _handle_skill_rollback(
        self,
        skill_name: str,
        candidate_id: str,
    ) -> str:
        """Rollback a skill to a previous version."""
        try:
            self._store.rollback(skill_name, candidate_id)
        except ValueError as e:
            return f"Rollback failed: {e}"

        # Apply the rolled-back candidate to the skill file
        candidate = self._store.get_active(skill_name)
        if candidate is None:
            return "Rollback succeeded but candidate could not be loaded."

        skill_path = self._get_skill_path(skill_name)
        previous_hash = ""
        if skill_path is not None:
            from arcagent.utils.io import atomic_write_text

            try:
                previous_text = skill_path.read_text(encoding="utf-8")
                previous_hash = Candidate(id="", text=previous_text).fingerprint
            except (OSError, FileNotFoundError):
                pass
            atomic_write_text(skill_path, candidate.text)

        # Set cooloff (use public property, not private _turn_number)
        current_turn = self._collector.turn_number if self._collector else 0
        self._guardrails.set_cooloff(
            skill_name,
            until_turn=current_turn + self._config.cooloff_turns,
        )

        # H-1/H-2: Full audit trail for rollback (NIST AU-3)
        rollback_event = MutationEvent(
            timestamp=datetime.now(UTC),
            skill_name=skill_name,
            previous_hash=previous_hash,
            new_hash=candidate.fingerprint,
            candidate_id=candidate_id,
            generation=candidate.generation,
            scores=dict(candidate.aggregate_scores),
            improvement={},
            stop_reason="rollback",
            trace_ids=[],
        )
        self._store.append_audit(skill_name, rollback_event)

        if self._telemetry:
            self._telemetry.audit_event(
                "skill_improver.rollback",
                {
                    "skill_name": skill_name,
                    "candidate_id": candidate_id,
                    "previous_hash": previous_hash,
                    "new_hash": candidate.fingerprint,
                },
            )

        return (
            f"Rolled back '{skill_name}' to candidate '{candidate_id}'. "
            f"Cooloff active for {self._config.cooloff_turns} turns."
        )
