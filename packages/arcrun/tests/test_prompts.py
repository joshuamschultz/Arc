"""Tests for strategy prompt provider — get_strategy_prompts().

arcrun owns strategy + arcrun-builtin tool guidance only. Tool guidance
for non-arcrun tools (e.g. spawn_task) lives with the tool's owner —
see arcagent.orchestration.prompts.SPAWN_GUIDANCE.
"""

from __future__ import annotations

import pytest

from arcrun.prompts import (
    CODE_EXEC_GUIDANCE,
    CONTAINED_EXEC_GUIDANCE,
    get_strategy_prompts,
)


class TestGetStrategyPromptsDefaults:
    """Default invocation returns core strategy guidance only."""

    def test_returns_dict(self) -> None:
        result = get_strategy_prompts()
        assert isinstance(result, dict)

    def test_includes_react_loop_guidance(self) -> None:
        result = get_strategy_prompts()
        assert "strategy_react" in result
        assert "Reason-Act-Observe" in result["strategy_react"]

    def test_no_spawn_guidance_in_arcrun(self) -> None:
        """spawn_task is owned by arcagent — arcrun must not emit its guidance."""
        result = get_strategy_prompts()
        assert "spawn_guidance" not in result

    def test_no_code_exec_guidance_by_default(self) -> None:
        result = get_strategy_prompts()
        assert "code_exec_guidance" not in result
        assert "contained_exec_guidance" not in result

    def test_no_strategy_selection_with_single_strategy(self) -> None:
        result = get_strategy_prompts()
        assert "strategy_selection" not in result


class TestCodeExecGuidance:
    """Code execution guidance is included when matching tools are present."""

    def test_execute_python_tool_triggers_guidance(self) -> None:
        result = get_strategy_prompts(tool_names=["execute_python"])
        assert "code_exec_guidance" in result
        assert result["code_exec_guidance"] == CODE_EXEC_GUIDANCE

    def test_contained_execute_python_tool_triggers_guidance(self) -> None:
        result = get_strategy_prompts(tool_names=["contained_execute_python"])
        assert "contained_exec_guidance" in result
        assert result["contained_exec_guidance"] == CONTAINED_EXEC_GUIDANCE

    def test_both_exec_tools_present(self) -> None:
        result = get_strategy_prompts(
            tool_names=["execute_python", "contained_execute_python"]
        )
        assert "code_exec_guidance" in result
        assert "contained_exec_guidance" in result

    def test_unrelated_tools_do_not_trigger(self) -> None:
        result = get_strategy_prompts(tool_names=["read", "write", "bash"])
        assert "code_exec_guidance" not in result
        assert "contained_exec_guidance" not in result

    def test_spawn_task_in_tool_names_does_not_trigger_arcrun_guidance(self) -> None:
        """arcrun has no spawn knowledge — passing spawn_task is a no-op here."""
        result = get_strategy_prompts(tool_names=["spawn_task"])
        assert "spawn_guidance" not in result


class TestMultipleStrategies:
    """Strategy selection guidance when multiple strategies are allowed."""

    def test_multiple_strategies_includes_selection_guidance(self) -> None:
        result = get_strategy_prompts(allowed_strategies=["react", "code"])
        assert "strategy_selection" in result
        assert "Strategy Selection" in result["strategy_selection"]

    def test_multiple_strategies_includes_both_guidance(self) -> None:
        result = get_strategy_prompts(allowed_strategies=["react", "code"])
        assert "strategy_react" in result
        assert "strategy_code" in result

    def test_single_strategy_no_selection(self) -> None:
        result = get_strategy_prompts(allowed_strategies=["react"])
        assert "strategy_selection" not in result

    def test_selection_guidance_lists_strategy_descriptions(self) -> None:
        result = get_strategy_prompts(allowed_strategies=["react", "code"])
        selection = result["strategy_selection"]
        assert "react" in selection
        assert "code" in selection

    def test_unknown_strategy_silently_skipped(self) -> None:
        """Unknown strategies don't crash, just omit their guidance."""
        result = get_strategy_prompts(allowed_strategies=["react", "nonexistent"])
        assert "strategy_react" in result
        assert "strategy_nonexistent" not in result


class TestStrategyPromptGuidanceProperty:
    """Each strategy class exposes prompt_guidance."""

    def test_react_strategy_has_prompt_guidance(self) -> None:
        from arcrun.strategies.react import ReactStrategy

        s = ReactStrategy()
        assert isinstance(s.prompt_guidance, str)
        assert len(s.prompt_guidance) > 50

    def test_code_strategy_has_prompt_guidance(self) -> None:
        from arcrun.strategies.code import CodeExecStrategy

        s = CodeExecStrategy()
        assert isinstance(s.prompt_guidance, str)
        assert len(s.prompt_guidance) > 50

    def test_react_guidance_describes_loop(self) -> None:
        from arcrun.strategies.react import ReactStrategy

        guidance = ReactStrategy().prompt_guidance
        assert "loop" in guidance.lower() or "Loop" in guidance

    def test_code_guidance_describes_python(self) -> None:
        from arcrun.strategies.code import CodeExecStrategy

        guidance = CodeExecStrategy().prompt_guidance
        assert "Python" in guidance or "code" in guidance.lower()


class TestStrategyABCPromptGuidance:
    """Strategy ABC enforces prompt_guidance as abstract."""

    def test_strategy_without_prompt_guidance_cannot_instantiate(self) -> None:
        from arcrun.strategies import Strategy

        class IncompleteStrategy(Strategy):
            @property
            def name(self) -> str:
                return "incomplete"

            @property
            def description(self) -> str:
                return "Missing prompt_guidance"

            async def __call__(self, model, state, sandbox, max_turns):
                pass

        with pytest.raises(TypeError):
            IncompleteStrategy()


class TestPromptGuidanceContent:
    """Validate prompt content quality — guidance must be decision-oriented."""

    def test_code_exec_guidance_has_prefer_criteria(self) -> None:
        """Code exec guidance should say when to prefer code vs tools."""
        assert "Prefer" in CODE_EXEC_GUIDANCE

    def test_all_arcrun_guidance_constants_are_nonempty(self) -> None:
        for guidance in (CODE_EXEC_GUIDANCE, CONTAINED_EXEC_GUIDANCE):
            assert isinstance(guidance, str)
            assert len(guidance) > 100
