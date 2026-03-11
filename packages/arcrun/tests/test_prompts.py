"""Tests for strategy prompt provider — get_strategy_prompts()."""

from __future__ import annotations

import pytest

from arcrun.prompts import (
    CODE_EXEC_GUIDANCE,
    CONTAINED_EXEC_GUIDANCE,
    SPAWN_GUIDANCE,
    get_strategy_prompts,
)


class TestGetStrategyPromptsDefaults:
    """Default invocation returns core guidance."""

    def test_returns_dict(self) -> None:
        result = get_strategy_prompts()
        assert isinstance(result, dict)

    def test_includes_react_loop_guidance(self) -> None:
        result = get_strategy_prompts()
        assert "strategy_react" in result
        assert "Reason-Act-Observe" in result["strategy_react"]

    def test_includes_spawn_guidance_by_default(self) -> None:
        result = get_strategy_prompts()
        assert "spawn_guidance" in result
        assert "spawn_task" in result["spawn_guidance"]

    def test_no_code_exec_guidance_by_default(self) -> None:
        result = get_strategy_prompts()
        assert "code_exec_guidance" not in result
        assert "contained_exec_guidance" not in result

    def test_no_strategy_selection_with_single_strategy(self) -> None:
        result = get_strategy_prompts()
        assert "strategy_selection" not in result


class TestSpawnGuidance:
    """Spawn guidance is controlled by spawn_enabled flag."""

    def test_spawn_enabled_true(self) -> None:
        result = get_strategy_prompts(spawn_enabled=True)
        assert "spawn_guidance" in result
        assert result["spawn_guidance"] == SPAWN_GUIDANCE

    def test_spawn_enabled_false(self) -> None:
        result = get_strategy_prompts(spawn_enabled=False)
        assert "spawn_guidance" not in result

    def test_spawn_guidance_contains_decision_gate(self) -> None:
        """Spawn guidance includes when-to and when-not-to criteria."""
        assert "Use spawn_task when:" in SPAWN_GUIDANCE
        assert "Do NOT use spawn_task when:" in SPAWN_GUIDANCE

    def test_spawn_guidance_contains_example(self) -> None:
        """Spawn guidance includes a few-shot delegation example."""
        assert "<example>" in SPAWN_GUIDANCE
        assert "Good delegation:" in SPAWN_GUIDANCE
        assert "Bad delegation:" in SPAWN_GUIDANCE


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

    def test_spawn_guidance_not_mechanical(self) -> None:
        """Guidance should be decisional, not just parameter docs."""
        # Should contain reasoning about WHEN, not just HOW
        assert "evaluate" in SPAWN_GUIDANCE.lower() or "when" in SPAWN_GUIDANCE.lower()

    def test_code_exec_guidance_has_prefer_criteria(self) -> None:
        """Code exec guidance should say when to prefer code vs tools."""
        assert "Prefer" in CODE_EXEC_GUIDANCE

    def test_all_guidance_constants_are_nonempty(self) -> None:
        for guidance in (SPAWN_GUIDANCE, CODE_EXEC_GUIDANCE, CONTAINED_EXEC_GUIDANCE):
            assert isinstance(guidance, str)
            assert len(guidance) > 100
