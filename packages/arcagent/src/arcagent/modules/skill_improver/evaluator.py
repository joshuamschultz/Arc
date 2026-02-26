"""LLM-as-Judge evaluator for skill procedures.

Evaluates skill text against execution traces using a per-dimension,
per-trace RaR (Rubrics as Rewards) checklist pattern. One LLM call
per dimension per trace for reliable scoring on a 1-5 scale.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol, runtime_checkable

from arcagent.modules.skill_improver.config import SkillImproverConfig
from arcagent.modules.skill_improver.models import (
    DimensionScore,
    EvalResult,
    SkillTrace,
)
from arcagent.utils.io import extract_json


@runtime_checkable
class LLMInvoker(Protocol):
    """Structural contract for LLM models used by the evaluator and reflector."""

    async def invoke(self, prompt: str) -> str: ...


_logger = logging.getLogger("arcagent.modules.skill_improver.evaluator")

# Per-dimension evaluation rubrics
DIMENSIONS: dict[str, dict[str, Any]] = {
    "accuracy": {
        "checklist": [
            "All steps lead to correct outcomes",
            "No incorrect or misleading instructions",
            "Prerequisites are correctly stated",
            "Success criteria are verifiable",
            "Edge cases are handled correctly",
        ],
        "anti_inflation": (
            "A score of 5 requires ZERO factual errors. Most procedures score 2-4."
        ),
    },
    "efficiency": {
        "checklist": [
            "No redundant or unnecessary steps",
            "Steps are in optimal order",
            "No unnecessary tool calls implied",
            "Procedure achieves goal in minimal steps",
            "No repeated information",
        ],
        "anti_inflation": (
            "A score of 5 requires every step to be essential. Most procedures score 2-4."
        ),
    },
    "error_handling": {
        "checklist": [
            "Common failure modes are anticipated",
            "Recovery steps are provided for errors",
            "Fallback paths are defined",
            "Error messages guide next actions",
            "Partial failure scenarios are addressed",
        ],
        "anti_inflation": (
            "A score of 5 requires comprehensive error coverage. Most procedures score 2-3."
        ),
    },
    "clarity": {
        "checklist": [
            "Each step has a single unambiguous action",
            "Technical terms are defined or standard",
            "Conditional branches specify both paths",
            "Success criteria are explicitly stated",
            "A practitioner can execute without interpretation",
        ],
        "anti_inflation": ("A score of 5 requires ZERO ambiguity. Most procedures score 2-4."),
    },
}


class SkillEvaluator:
    """Evaluate skill procedures against execution traces using LLM-as-judge."""

    def __init__(self, config: SkillImproverConfig, llm: LLMInvoker) -> None:
        self._config = config
        self._llm = llm

    def build_judge_prompt(
        self,
        skill_text: str,
        trace: SkillTrace,
        dimension: str,
    ) -> str:
        """Construct the judge prompt for a single dimension evaluation."""
        dim_config = DIMENSIONS.get(dimension, DIMENSIONS["accuracy"])
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

        return f"""\
You are evaluating a skill procedure document on {dimension}.

CALIBRATION:
Score 1 (Poor): Procedure fails on this dimension in most scenarios.
Score 3 (Moderate): Procedure is adequate but has notable gaps.
Score 5 (Excellent): Procedure excels — no issues on this dimension.

{anti_inflation}

CHECKLIST (answer YES or NO for each):
{checklist_text}

EXECUTION TRACE:
Task: {trace.task_summary}
Tool calls:
{tool_calls_text}
Errors: {errors_text}
Outcome: {trace.task_outcome or "unknown"}
Coverage: {trace.coverage_pct:.0f}%

PROCEDURE TO EVALUATE:
{skill_text}

First, evaluate each checklist item with YES/NO and brief reasoning.
Then provide your score (1-5) = count of YES answers.

Respond in JSON:
{{"checklist": [{{"item": str, "answer": bool, "reason": str}}],
"score": int, "rationale": str}}"""

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
