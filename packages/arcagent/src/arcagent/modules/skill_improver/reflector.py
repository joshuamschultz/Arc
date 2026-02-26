"""Skill reflector — constrained mutation via LLM reflection.

Analyzes failure patterns in execution traces and proposes targeted
improvements to skill procedure text. Mutations are constrained to
specific sections and must preserve the immutable intent header.
"""

from __future__ import annotations

import logging
import re
from collections import Counter

from arcagent.modules.skill_improver.config import SkillImproverConfig
from arcagent.modules.skill_improver.evaluator import LLMInvoker
from arcagent.modules.skill_improver.models import DimensionScore, SkillTrace
from arcagent.utils.sanitizer import sanitize_text

_logger = logging.getLogger("arcagent.modules.skill_improver.reflector")

_MARKDOWN_FENCE_RE = re.compile(r"```(?:markdown)?\s*\n(.*?)\n```", re.DOTALL)


class SkillReflector:
    """Propose constrained mutations to skill text based on failure analysis."""

    def __init__(self, config: SkillImproverConfig, llm: LLMInvoker) -> None:
        self._config = config
        self._llm = llm

    def build_reflection_prompt(
        self,
        current_text: str,
        weak_dimensions: list[str],
        failure_patterns: list[str],
        intent_header: str,
        token_budget: int,
    ) -> str:
        """Construct the reflection prompt for constrained mutation."""
        patterns_text = (
            "\n".join(f"- {p}" for p in failure_patterns)
            if failure_patterns
            else "None identified"
        )
        dims_text = ", ".join(weak_dimensions) if weak_dimensions else "general"

        return f"""\
You are improving a skill procedure document.

RULES:
- DO NOT modify the SKILL INTENT [IMMUTABLE] section
- Focus your revision ONLY on: {dims_text}
- DO NOT add unnecessary caveats or hedging language
- The revised skill must be under {token_budget} tokens
- Produce specific, actionable steps — not descriptions

CURRENT SKILL:
{current_text}

FAILURE PATTERNS (across execution traces):
{patterns_text}

WEAKEST DIMENSIONS:
{dims_text}

Identify the root cause pattern across these failures.
Then produce an improved version of the skill inside ```markdown``` fences."""

    def extract_candidate(self, response: str) -> str:
        """Extract candidate skill text from LLM response.

        Looks for ```markdown``` fences first, falls back to full response.
        Sanitizes output to defend against LLM output injection (LLM05, ASI-01).
        """
        if not response:
            return ""
        match = _MARKDOWN_FENCE_RE.search(response)
        raw = match.group(1).strip() if match else response.strip()
        # Sanitize: strip zero-width chars, control chars, normalize (ASI-06)
        return sanitize_text(raw, max_length=50_000)

    def identify_weak_dimensions(
        self,
        failures: list[tuple[SkillTrace, dict[str, DimensionScore]]],
    ) -> list[str]:
        """Find dimensions with lowest average scores across failures."""
        dim_totals: dict[str, list[float]] = {}
        for _trace, scores in failures:
            for dim, ds in scores.items():
                dim_totals.setdefault(dim, []).append(float(ds.score))

        dim_averages = {dim: sum(vals) / len(vals) for dim, vals in dim_totals.items()}
        # Sort by average score ascending (weakest first)
        return sorted(dim_averages, key=lambda d: dim_averages[d])

    def extract_failure_patterns(self, traces: list[SkillTrace]) -> list[str]:
        """Group failures by common error patterns."""
        error_counts: Counter[str] = Counter()
        for trace in traces:
            for tc in trace.tool_calls:
                if tc.error_type:
                    error_counts[f"{tc.error_type} in {tc.tool_name} calls"] += 1

        if not error_counts:
            return []
        # Return patterns sorted by frequency
        return [pattern for pattern, _ in error_counts.most_common()]

    async def reflect(
        self,
        current_text: str,
        failures: list[tuple[SkillTrace, dict[str, DimensionScore]]],
        intent_header: str,
        token_budget: int,
    ) -> str:
        """Full reflection pipeline: analyze failures, propose mutation."""
        weak_dims = self.identify_weak_dimensions(failures)
        traces = [t for t, _ in failures]
        patterns = self.extract_failure_patterns(traces)

        prompt = self.build_reflection_prompt(
            current_text,
            weak_dims,
            patterns,
            intent_header,
            token_budget,
        )
        try:
            response = await self._llm.invoke(prompt)
        except (OSError, TimeoutError, ConnectionError, RuntimeError):
            _logger.exception("LLM error during reflection")
            return ""
        return self.extract_candidate(response)
