"""Skill reflector — constrained mutation via LLM reflection.

Analyzes failure patterns in execution traces and proposes targeted
improvements to skill procedure text. Mutations are constrained to
specific sections and must preserve the immutable intent header.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter

from arcskill.improver._util import sanitize_text
from arcskill.improver.config import ImproverConfig
from arcskill.improver.models import BundlePatch, BundleView, DimensionScore, SkillTrace
from arcskill.improver.seams import LLMInvoker

_logger = logging.getLogger("arcskill.improver.mutate")

_MARKDOWN_FENCE_RE = re.compile(r"```(?:markdown)?\s*\n(.*?)\n```", re.DOTALL)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


class SkillReflector:
    """Propose constrained mutations to skill text based on failure analysis."""

    def __init__(self, config: ImproverConfig, llm: LLMInvoker) -> None:
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


class LLMCodeMutator:
    """Default code-repair :class:`~arcskill.improver.seams.Mutator` (REQ-010/011, GEPA).

    Turns failing-trace error signals into a multi-file :class:`BundlePatch` over the
    skill's *existing* script files via one bounded LLM call. Least-privilege: the patch
    may only replace files already in the bundle — it can never create new files or paths
    (ASI05). Provider-free: the LLM enters through the injected :class:`LLMInvoker` seam.
    """

    def __init__(self, llm: LLMInvoker) -> None:
        self._llm = llm

    async def propose(
        self, *, kind: str, current: BundleView, failures: str, insight: str
    ) -> BundlePatch | None:
        if kind != "code" or not current.scripts:
            return None
        prompt = self._build_prompt(current.scripts, failures, insight)
        try:
            response = await self._llm.invoke(prompt)
        except (OSError, TimeoutError, ConnectionError, RuntimeError):
            _logger.exception("LLM error during code repair")
            return None
        return self._parse_patch(response, current.scripts)

    def _build_prompt(self, scripts: dict[str, bytes], failures: str, insight: str) -> str:
        files_text = "\n\n".join(
            f"### {rel}\n```python\n{data.decode('utf-8', 'replace')}\n```"
            for rel, data in scripts.items()
        )
        insight_block = f"\nRECURRING-FAILURE INSIGHT:\n{insight}\n" if insight else ""
        return f"""\
You are repairing the CODE of an agent skill. Fix the root cause of the failures below.

RULES:
- Only modify the files shown; do NOT add new files or paths.
- Return ONLY a JSON object:
  {{"files": {{"<path>": "<full new file content>"}}, "summary": "<why>"}}
- Include a file only if you changed it. Preserve behavior that already works.

FAILURES (from execution traces):
{failures}
{insight_block}
CURRENT FILES:
{files_text}
"""

    def _parse_patch(self, response: str, scripts: dict[str, bytes]) -> BundlePatch | None:
        if not response:
            return None
        match = _JSON_FENCE_RE.search(response)
        raw = match.group(1) if match else response
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            _logger.warning("code mutator returned unparseable JSON")
            return None
        files_raw = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files_raw, dict) or not files_raw:
            return None
        files: dict[str, bytes] = {}
        for rel, content in files_raw.items():
            if rel not in scripts or not isinstance(content, str):
                continue  # least-privilege: never create files outside the bundle
            files[rel] = sanitize_text(content, max_length=100_000).encode("utf-8")
        if not files:
            return None
        summary = str(payload.get("summary", "")) if isinstance(payload, dict) else ""
        return BundlePatch(files=files, summary=summary[:500])


__all__ = ["LLMCodeMutator", "SkillReflector"]
