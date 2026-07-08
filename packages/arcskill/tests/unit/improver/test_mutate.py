"""Tests for SkillReflector — constrained mutation via LLM reflection."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from arcskill.improver.config import ImproverConfig
from arcskill.improver.models import DimensionScore, SkillTrace, ToolCallRecord
from arcskill.improver.mutate import SkillReflector


def _make_trace(outcome: str = "failure") -> SkillTrace:
    return SkillTrace(
        trace_id="trace-1",
        session_id="s1",
        skill_name="test-skill",
        skill_version=0,
        turn_number=1,
        started_at=datetime(2026, 2, 25, 10, 0, 0, tzinfo=UTC),
        ended_at=datetime(2026, 2, 25, 10, 1, 0, tzinfo=UTC),
        tool_calls=[
            ToolCallRecord(
                tool_name="bash",
                args_hash="h1",
                result_status="error",
                duration_ms=50.0,
                error_type="TimeoutError",
            ),
        ],
        task_summary="Plan a trip",
        task_outcome=outcome,
    )


SKILL_TEXT = """\
## SKILL INTENT [IMMUTABLE]
Plan business travel efficiently.

## Steps
1. Check calendar
2. Book flights
3. Confirm hotel
"""


def _make_dim_score(dim: str, score: int) -> DimensionScore:
    return DimensionScore(dimension=dim, score=score)


class TestReflectionPrompt:
    """F1: Constrained, section-targeted, token budget, intent preservation."""

    def test_prompt_contains_skill_text(self) -> None:
        config = ImproverConfig()
        r = SkillReflector(config, llm=AsyncMock())
        prompt = r.build_reflection_prompt(
            SKILL_TEXT,
            weak_dimensions=["error_handling"],
            failure_patterns=["TimeoutError in bash calls"],
            intent_header="Plan business travel efficiently.",
            token_budget=100,
        )
        assert "Check calendar" in prompt

    def test_prompt_preserves_intent_instruction(self) -> None:
        config = ImproverConfig()
        r = SkillReflector(config, llm=AsyncMock())
        prompt = r.build_reflection_prompt(
            SKILL_TEXT,
            weak_dimensions=["accuracy"],
            failure_patterns=[],
            intent_header="Plan business travel efficiently.",
            token_budget=100,
        )
        assert "IMMUTABLE" in prompt or "DO NOT modify" in prompt

    def test_prompt_contains_token_budget(self) -> None:
        config = ImproverConfig()
        r = SkillReflector(config, llm=AsyncMock())
        prompt = r.build_reflection_prompt(
            SKILL_TEXT,
            weak_dimensions=["clarity"],
            failure_patterns=[],
            intent_header="intent",
            token_budget=150,
        )
        assert "150" in prompt

    def test_prompt_targets_weak_dimensions(self) -> None:
        config = ImproverConfig()
        r = SkillReflector(config, llm=AsyncMock())
        prompt = r.build_reflection_prompt(
            SKILL_TEXT,
            weak_dimensions=["error_handling", "clarity"],
            failure_patterns=[],
            intent_header="intent",
            token_budget=100,
        )
        assert "error_handling" in prompt
        assert "clarity" in prompt


class TestCandidateExtraction:
    """F2: Extract candidate text from LLM response (markdown fences)."""

    def test_extract_from_markdown_fences(self) -> None:
        config = ImproverConfig()
        r = SkillReflector(config, llm=AsyncMock())
        response = (
            "Here is the improved skill:\n```markdown\n# Better Skill\nDo stuff.\n```\nDone."
        )
        text = r.extract_candidate(response)
        assert text == "# Better Skill\nDo stuff."

    def test_extract_without_fences(self) -> None:
        config = ImproverConfig()
        r = SkillReflector(config, llm=AsyncMock())
        response = "# Improved Skill\nDo things better."
        text = r.extract_candidate(response)
        assert "Improved Skill" in text

    def test_extract_empty_response(self) -> None:
        config = ImproverConfig()
        r = SkillReflector(config, llm=AsyncMock())
        text = r.extract_candidate("")
        assert text == ""


class TestWeakDimensionIdentification:
    """F3: Finds dimensions with lowest scores."""

    def test_identify_weakest(self) -> None:
        config = ImproverConfig()
        r = SkillReflector(config, llm=AsyncMock())
        failures = [
            (
                _make_trace(),
                {
                    "accuracy": _make_dim_score("accuracy", 4),
                    "clarity": _make_dim_score("clarity", 1),
                },
            ),
            (
                _make_trace(),
                {
                    "accuracy": _make_dim_score("accuracy", 3),
                    "clarity": _make_dim_score("clarity", 2),
                },
            ),
        ]
        weak = r.identify_weak_dimensions(failures)
        assert weak[0] == "clarity"  # Lowest average

    def test_identify_weakest_multiple(self) -> None:
        config = ImproverConfig()
        r = SkillReflector(config, llm=AsyncMock())
        failures = [
            (
                _make_trace(),
                {
                    "accuracy": _make_dim_score("accuracy", 1),
                    "clarity": _make_dim_score("clarity", 1),
                    "efficiency": _make_dim_score("efficiency", 4),
                },
            ),
        ]
        weak = r.identify_weak_dimensions(failures)
        assert "accuracy" in weak[:2]
        assert "clarity" in weak[:2]


class TestFailurePatterns:
    """F4: Groups failures by common patterns."""

    def test_extract_error_patterns(self) -> None:
        config = ImproverConfig()
        r = SkillReflector(config, llm=AsyncMock())
        traces = [_make_trace(), _make_trace()]
        patterns = r.extract_failure_patterns(traces)
        assert any("TimeoutError" in p for p in patterns)

    def test_extract_patterns_no_errors(self) -> None:
        config = ImproverConfig()
        r = SkillReflector(config, llm=AsyncMock())
        trace = SkillTrace(
            trace_id="t1",
            session_id="s1",
            skill_name="test",
            skill_version=0,
            turn_number=1,
            started_at=datetime(2026, 2, 25, 10, 0, 0, tzinfo=UTC),
            tool_calls=[
                ToolCallRecord(
                    tool_name="read",
                    args_hash="h",
                    result_status="ok",
                    duration_ms=5.0,
                ),
            ],
            task_outcome="success",
        )
        patterns = r.extract_failure_patterns([trace])
        assert patterns == [] or all("error" not in p.lower() for p in patterns)


class TestReflect:
    """F5: Full reflect pipeline (mock LLM)."""

    @pytest.mark.asyncio
    async def test_reflect_returns_improved_text(self) -> None:
        config = ImproverConfig()
        mock_llm = AsyncMock()
        mock_llm.invoke.return_value = (
            "```markdown\n"
            "## SKILL INTENT [IMMUTABLE]\n"
            "Plan business travel efficiently.\n\n"
            "## Steps\n"
            "1. Check calendar availability\n"
            "2. Search flights with timeout handling\n"
            "3. Book and confirm hotel\n"
            "```"
        )
        r = SkillReflector(config, llm=mock_llm)
        failures = [
            (_make_trace(), {"accuracy": _make_dim_score("accuracy", 2)}),
        ]
        result = await r.reflect(
            SKILL_TEXT,
            failures,
            "Plan business travel efficiently.",
            token_budget=200,
        )
        assert "timeout handling" in result.lower() or "calendar" in result.lower()
        mock_llm.invoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_reflect_handles_llm_error(self) -> None:
        config = ImproverConfig()
        mock_llm = AsyncMock()
        mock_llm.invoke.side_effect = RuntimeError("LLM error")
        r = SkillReflector(config, llm=mock_llm)
        failures = [(_make_trace(), {"accuracy": _make_dim_score("accuracy", 1)})]
        result = await r.reflect(SKILL_TEXT, failures, "intent", token_budget=100)
        assert result == ""  # Empty on error


# ---------------------------------------------------------------------------
# LLMCodeMutator — code-repair patch proposer (SPEC-044 P4)
# ---------------------------------------------------------------------------


class TestLLMCodeMutator:
    """The default code-repair Mutator parses a bounded, least-privilege patch."""

    def _view(self):
        from arcskill.improver.models import BundleView

        return BundleView(
            "calc",
            "# Calc\n",
            None,
            scripts={"scripts/calc.py": b"def add(a, b):\n    return a - b\n"},
        )

    @pytest.mark.asyncio
    async def test_parses_json_patch_over_existing_file(self):
        from unittest.mock import AsyncMock

        from arcskill.improver.mutate import LLMCodeMutator

        mock_llm = AsyncMock()
        mock_llm.invoke.return_value = (
            '```json\n{"files": {"scripts/calc.py": "def add(a, b):\\n    return a + b\\n"},'
            ' "summary": "fix"}\n```'
        )
        patch = await LLMCodeMutator(mock_llm).propose(
            kind="code", current=self._view(), failures="AssertionError", insight=""
        )
        assert patch is not None
        assert patch.files["scripts/calc.py"] == b"def add(a, b):\n    return a + b\n"
        assert patch.summary == "fix"

    @pytest.mark.asyncio
    async def test_rejects_files_outside_bundle(self):
        from unittest.mock import AsyncMock

        from arcskill.improver.mutate import LLMCodeMutator

        mock_llm = AsyncMock()
        mock_llm.invoke.return_value = '{"files": {"/etc/passwd": "x", "new.py": "y"}}'
        patch = await LLMCodeMutator(mock_llm).propose(
            kind="code", current=self._view(), failures="e", insight=""
        )
        assert patch is None  # least-privilege: no existing file touched

    @pytest.mark.asyncio
    async def test_non_code_kind_returns_none(self):
        from unittest.mock import AsyncMock

        from arcskill.improver.mutate import LLMCodeMutator

        patch = await LLMCodeMutator(AsyncMock()).propose(
            kind="prose", current=self._view(), failures="e", insight=""
        )
        assert patch is None

    @pytest.mark.asyncio
    async def test_unparseable_response_returns_none(self):
        from unittest.mock import AsyncMock

        from arcskill.improver.mutate import LLMCodeMutator

        mock_llm = AsyncMock()
        mock_llm.invoke.return_value = "not json at all"
        patch = await LLMCodeMutator(mock_llm).propose(
            kind="code", current=self._view(), failures="e", insight=""
        )
        assert patch is None
