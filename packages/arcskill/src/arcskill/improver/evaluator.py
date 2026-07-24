"""LLM-as-Judge evaluator for skill procedures.

Evaluates skill text against execution traces using a per-dimension,
per-trace RaR (Rubrics as Rewards) checklist pattern. One LLM call
per dimension per trace for reliable scoring on a 1-5 scale.
"""

from __future__ import annotations

import json
import logging

from arcskill.context import PromptResolve, load_prompt, load_rubric
from arcskill.improver._util import extract_json
from arcskill.improver.config import ImproverConfig
from arcskill.improver.models import (
    DimensionScore,
    EvalResult,
    SkillTrace,
)
from arcskill.improver.seams import LLMInvoker

_logger = logging.getLogger("arcskill.improver.evaluator")


class SkillEvaluator:
    """Evaluate skill procedures against execution traces using LLM-as-judge."""

    def __init__(
        self, config: ImproverConfig, llm: LLMInvoker, *, resolve: PromptResolve | None = None
    ) -> None:
        self._config = config
        self._llm = llm
        self._resolve = resolve
        # Load the scoring rubric once per pass — overlay-aware when the arc system
        # is present (an operator edit wins), stock otherwise. Frozen for the pass.
        self._rubric = load_rubric(resolve=resolve)

    def build_judge_prompt(
        self,
        skill_text: str,
        trace: SkillTrace,
        dimension: str,
    ) -> str:
        """Construct the judge prompt for a single dimension evaluation."""
        dim_config = self._rubric.get(dimension, self._rubric["accuracy"])
        checklist = dim_config["checklist"]
        anti_inflation = dim_config["anti_inflation"]

        checklist_text = "\n".join(f"- {item} (YES or NO)" for item in checklist)

        tool_calls_text = "\n".join(
            f"  - {tc.tool_name}: status={tc.result_status}, "
            f"duration={tc.duration_ms:.0f}ms"
            + (f", error={tc.error_type}" if tc.error_type else "")
            for tc in trace.tool_calls
        )
        errors_text = (
            ", ".join(tc.error_type for tc in trace.tool_calls if tc.error_type) or "None"
        )

        return load_prompt("judge_prompt", resolve=self._resolve).format(
            dimension=dimension,
            anti_inflation=anti_inflation,
            checklist_text=checklist_text,
            task_summary=trace.task_summary,
            tool_calls_text=tool_calls_text,
            errors_text=errors_text,
            task_outcome=trace.task_outcome or "unknown",
            coverage_pct=trace.coverage_pct,
            skill_text=skill_text,
        )

    def parse_score(self, response: str, dimension: str) -> DimensionScore:
        """Parse LLM judge response into a DimensionScore.

        Handles JSON fences, missing fields, and invalid values gracefully.
        Falls back to score=1 on any parse failure.
        """
        if not response:
            return DimensionScore(dimension=dimension, score=1, rationale="Empty response")

        try:
            cleaned = extract_json(response)
            data = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            _logger.warning("Failed to parse judge response for %s", dimension)
            return DimensionScore(dimension=dimension, score=1, rationale="Parse error")

        raw_score = data.get("score")
        if raw_score is None:
            return DimensionScore(dimension=dimension, score=1, rationale="Missing score")

        score = max(1, min(self._config.eval_scale, int(raw_score)))
        checklist = data.get("checklist", [])
        rationale = str(data.get("rationale", ""))

        return DimensionScore(
            dimension=dimension,
            score=score,
            checklist_results=checklist,
            rationale=rationale,
        )

    async def evaluate_dimension(
        self,
        skill_text: str,
        trace: SkillTrace,
        dimension: str,
    ) -> DimensionScore:
        """Evaluate a single dimension for a single trace. One LLM call."""
        prompt = self.build_judge_prompt(skill_text, trace, dimension)
        try:
            response = await self._llm.invoke(prompt)
        except (OSError, TimeoutError, ConnectionError, RuntimeError):
            _logger.exception("LLM error evaluating %s", dimension)
            return DimensionScore(dimension=dimension, score=1, rationale="LLM error")
        return self.parse_score(response, dimension)

    async def evaluate(
        self,
        skill_text: str,
        traces: list[SkillTrace],
    ) -> EvalResult:
        """Evaluate skill across all configured dimensions and traces."""
        per_trace_scores: list[dict[str, DimensionScore]] = []
        for trace in traces:
            dim_scores: dict[str, DimensionScore] = {}
            for dim in self._config.eval_dimensions:
                dim_scores[dim] = await self.evaluate_dimension(skill_text, trace, dim)
            per_trace_scores.append(dim_scores)
        return EvalResult(per_trace_scores=per_trace_scores)
