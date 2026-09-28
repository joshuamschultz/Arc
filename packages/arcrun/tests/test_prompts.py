"""Tests for the strategy prompt properties on the ``Strategy`` ABC.

A run resolves each strategy's prompts through its ``PromptSource``
(``test_strategy_selection_prompt.py``); these properties are the shipped-file
fallback and the plain listing a caller with no run prints.
"""

from __future__ import annotations

import pytest
from arcprompt import load_stock


class TestStrategyPromptGuidanceProperty:
    """Each strategy class exposes prompt_guidance."""

    def test_react_strategy_has_prompt_guidance(self) -> None:
        from arcrun.strategies.react import ReactStrategy

        s = ReactStrategy()
        assert s.prompt_guidance == load_stock("arcrun", "strategy_react")

    def test_code_strategy_has_prompt_guidance(self) -> None:
        from arcrun.strategies.code import CodeExecStrategy

        s = CodeExecStrategy()
        assert s.prompt_guidance == load_stock("arcrun", "strategy_code")

    def test_react_guidance_describes_loop(self) -> None:
        from arcrun.strategies.react import ReactStrategy

        guidance = ReactStrategy().prompt_guidance
        assert "loop" in guidance.lower() or "Loop" in guidance

    def test_code_guidance_describes_python(self) -> None:
        from arcrun.strategies.code import CodeExecStrategy

        guidance = CodeExecStrategy().prompt_guidance
        assert "Python" in guidance or "code" in guidance.lower()


class TestStrategyABCContract:
    """The ABC requires ``name`` + ``__call__``; the prompt is markdown-backed.

    A strategy's copy lives as ``strategy_<name>[_description]`` markdown, so
    ``description`` and ``prompt_guidance`` default to loading it — a custom
    strategy needs no inline Python prompt. Only the behavioural surface
    (``name`` and the loop body) stays abstract.
    """

    def test_strategy_without_name_cannot_instantiate(self) -> None:
        from arcrun.strategies import Strategy

        class NoName(Strategy):
            async def __call__(self, model, state, sandbox, max_turns):  # type: ignore[no-untyped-def]
                return None

        with pytest.raises(TypeError):
            NoName()  # type: ignore[abstract]

    def test_a_bare_strategy_loads_with_markdown_backed_prompts(self) -> None:
        from arcrun.strategies import Strategy

        class Bare(Strategy):
            @property
            def name(self) -> str:
                return "bare_probe_no_md"

            async def __call__(self, model, state, sandbox, max_turns):  # type: ignore[no-untyped-def]
                return None

        strategy = Bare()
        # No strategy_bare_probe_no_md.md ships, so the default resolves to "" —
        # a bare strategy still constructs and reports empty prompt, not a crash.
        assert strategy.prompt_guidance == ""
        assert strategy.description == ""
