"""The bounded one-shot strategy — cheap inference with a first-class home (ADR-032).

Classification gates need a model call without a loop, without tools, and
without a sandbox. That need is legitimate; reaching a provider handle from a
higher layer to serve it is not. These tests pin the shape that replaces it: a
named strategy arcrun owns, an entry point on the arcrun facade, and a hard
bound on what one call may cost.
"""

from __future__ import annotations

import asyncio

import pytest
from packages.arcrun.tests.conftest import LLMResponse, MockModel, Usage

import arcrun


class TestOneShotStrategy:
    def test_it_is_registered_under_a_stable_name(self) -> None:
        assert "oneshot" in arcrun.available_strategies()

    def test_it_is_a_strategy(self) -> None:
        from arcrun.strategies.oneshot import OneShotStrategy

        assert issubclass(OneShotStrategy, arcrun.Strategy)
        assert OneShotStrategy().name == "oneshot"

    def test_it_describes_itself_for_prompt_assembly(self) -> None:
        from arcrun.strategies.oneshot import OneShotStrategy

        strategy = OneShotStrategy()

        assert strategy.description.strip()
        assert strategy.prompt_guidance.strip()

    def test_the_model_is_never_offered_it_by_auto_selection(self) -> None:
        """A one-shot has no loop and no tools; auto-picking it would gut a run.

        ``allowed_strategies=None`` means "every strategy installed is on the
        table", which is right for agentic shapes and wrong for this one. It
        stays reachable only when a caller names it.
        """
        from arcrun.strategies.oneshot import OneShotStrategy
        from arcrun.strategies.react import ReactStrategy

        assert OneShotStrategy().auto_selectable is False
        assert ReactStrategy().auto_selectable is True

    @pytest.mark.asyncio
    async def test_auto_selection_does_not_offer_it_to_the_model(self) -> None:
        from arcrun._messages import system_message, user_message
        from arcrun.events import EventBus
        from arcrun.registry import ToolRegistry
        from arcrun.state import RunState
        from arcrun.strategies import select_strategy

        model = MockModel([], strategy="react")
        bus = EventBus(run_id="test")
        state = RunState(
            messages=[system_message("s"), user_message("u")],
            registry=ToolRegistry(tools=[], event_bus=bus),
            event_bus=bus,
        )

        await select_strategy(None, model, state)

        offered = state.event_bus.events[0].data["allowed_strategies"]
        assert "oneshot" not in offered
        assert "react" in offered


class TestRunOneshot:
    @pytest.mark.asyncio
    async def test_it_makes_exactly_one_call_with_no_tools(self) -> None:
        model = MockModel([LLMResponse(content="YES", stop_reason="end_turn")])

        result = await arcrun.run_oneshot(model, system="rules", user="question")

        assert result.content == "YES"
        assert len(model.invoke_calls) == 1
        assert not model.invoke_calls[0]["tools"]

    @pytest.mark.asyncio
    async def test_it_bounds_the_output_tokens(self) -> None:
        model = MockModel([LLMResponse(content="YES", stop_reason="end_turn")])

        await arcrun.run_oneshot(model, system="rules", user="question", max_tokens=8)

        assert model.invoke_calls[0]["kwargs"]["max_tokens"] == 8

    @pytest.mark.asyncio
    async def test_it_sends_the_system_and_user_turn_in_order(self) -> None:
        model = MockModel([LLMResponse(content="NO", stop_reason="end_turn")])

        await arcrun.run_oneshot(model, system="rules", user="question")

        messages = model.invoke_calls[0]["messages"]
        assert [m.role for m in messages] == ["system", "user"]

    @pytest.mark.asyncio
    async def test_it_reports_what_the_call_cost(self) -> None:
        """The bypass it replaces dropped usage on the floor, hiding the spend."""
        model = MockModel(
            [
                LLMResponse(
                    content="YES",
                    stop_reason="end_turn",
                    usage=Usage(input_tokens=120, output_tokens=2, total_tokens=122),
                    cost_usd=0.0004,
                )
            ]
        )

        result = await arcrun.run_oneshot(model, system="rules", user="question")

        assert result.tokens_used["total"] == 122
        assert result.cost_usd == pytest.approx(0.0004)
        assert result.strategy_used == "oneshot"

    @pytest.mark.asyncio
    async def test_a_hung_provider_raises_rather_than_blocking_forever(self) -> None:
        class Hanging:
            async def invoke(self, messages: list[object], **kwargs: object) -> object:
                await asyncio.sleep(30)
                raise AssertionError("unreachable")

        with pytest.raises(TimeoutError):
            await arcrun.run_oneshot(Hanging(), system="s", user="u", timeout=0.01)

    @pytest.mark.asyncio
    async def test_it_emits_a_terminal_event_for_the_audit_chain(self) -> None:
        model = MockModel([LLMResponse(content="YES", stop_reason="end_turn")])

        result = await arcrun.run_oneshot(model, system="rules", user="question")

        assert [e.type for e in result.events][-1] == "loop.complete"
