"""``choose_strategy``: the strategy is picked first, from cheap inputs only."""

from __future__ import annotations

from typing import Any

import pytest
from arcllm import LLMResponse, ToolCall, Usage
from arcllm.exceptions import ArcLLMError

from arcrun import choose_strategy


class _Model:
    def __init__(self, response: LLMResponse | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def invoke(self, messages: list[Any], tools: list[Any] | None = None, **kw: Any) -> Any:
        self.calls.append({"messages": messages, "tools": tools})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _pick(name: str, why: str = "fits") -> LLMResponse:
    return LLMResponse(
        tool_calls=[
            ToolCall(id="s", name="select_strategy", arguments={"strategy": name, "reasoning": why})
        ],
        stop_reason="tool_use",
        model="m",
        usage=Usage(input_tokens=0, output_tokens=0, total_tokens=0),
    )


@pytest.mark.asyncio
async def test_one_allowed_strategy_needs_no_model_call() -> None:
    model = _Model(_pick("code"))
    choice = await choose_strategy(["react"], model, task="hi")
    assert (choice.name, choice.selected_by) == ("react", "only")
    assert model.calls == []


@pytest.mark.asyncio
async def test_model_choice_carries_its_reason_and_sees_recent_turns() -> None:
    model = _Model(_pick("code", "needs a script"))
    choice = await choose_strategy(
        ["react", "code"], model, task="crunch the csv", recent=["earlier ask", "earlier answer"]
    )
    assert (choice.name, choice.reason, choice.selected_by) == ("code", "needs a script", "model")
    sent = model.calls[0]["messages"][-1].content
    text = sent if isinstance(sent, str) else str(sent)
    assert "crunch the csv" in text
    assert "earlier ask" in text


@pytest.mark.asyncio
async def test_failed_selection_falls_back_to_react_with_a_reason() -> None:
    choice = await choose_strategy(["react", "code"], _Model(ArcLLMError("down")), task="x")
    assert (choice.name, choice.selected_by) == ("react", "fallback")
    assert "ArcLLMError" in choice.reason


@pytest.mark.asyncio
async def test_a_slow_selection_call_falls_back_to_react_at_the_timeout() -> None:
    import asyncio

    class _Hung(_Model):
        async def invoke(self, messages: list[Any], tools: Any = None, **kw: Any) -> Any:
            await asyncio.Event().wait()

    choice = await choose_strategy(["react", "code"], _Hung(_pick("code")), task="x", timeout=0.05)
    assert (choice.name, choice.selected_by) == ("react", "fallback")
    assert "TimeoutError" in choice.reason


@pytest.mark.asyncio
async def test_unknown_choice_falls_back_to_react() -> None:
    choice = await choose_strategy(["react", "code"], _Model(_pick("bogus")), task="x")
    assert (choice.name, choice.selected_by) == ("react", "fallback")
