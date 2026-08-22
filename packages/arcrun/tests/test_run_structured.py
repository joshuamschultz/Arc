"""``run_structured`` — the forced-tool structured sibling of ``run_oneshot``.

A caller that needs a schema filled (a plan DAG, a labelled extraction) must be
able to force one tool call and read its arguments without holding a provider
handle (ADR-032). These pin that entry: it forces the named tool, hands back the
arguments, bounds the call, and refuses an answer that skipped the tool.
"""

from __future__ import annotations

import asyncio

import pytest
from packages.arcrun.tests.conftest import LLMResponse, MockModel

import arcrun

pytestmark = pytest.mark.asyncio


def _tool() -> arcrun.ModelTool:
    return arcrun.ModelTool(
        name="emit_plan",
        description="Emit a plan.",
        parameters={"type": "object", "properties": {"steps": {"type": "array"}}},
    )


def _messages() -> list[arcrun.Message]:
    return [arcrun.Message(role="user", content="plan it")]


async def test_it_returns_the_forced_tool_call_arguments() -> None:
    args = {"steps": [{"step_id": "a"}]}
    model = MockModel(
        [LLMResponse(tool_calls=[arcrun.ToolCall(id="1", name="emit_plan", arguments=args)])]
    )

    out = await arcrun.run_structured(model, _messages(), tool=_tool())

    assert out == args


async def test_it_forces_exactly_that_tool() -> None:
    model = MockModel(
        [LLMResponse(tool_calls=[arcrun.ToolCall(id="1", name="emit_plan", arguments={})])]
    )

    await arcrun.run_structured(model, _messages(), tool=_tool(), max_tokens=256)

    call = model.task_calls[-1]
    assert [t.name for t in call["tools"]] == ["emit_plan"]
    assert call["kwargs"]["tool_choice"] == {"type": "tool", "name": "emit_plan"}
    assert call["kwargs"]["max_tokens"] == 256


async def test_no_ceiling_sends_no_max_tokens() -> None:
    model = MockModel(
        [LLMResponse(tool_calls=[arcrun.ToolCall(id="1", name="emit_plan", arguments={})])]
    )

    await arcrun.run_structured(model, _messages(), tool=_tool())

    assert "max_tokens" not in model.task_calls[-1]["kwargs"]


async def test_it_raises_when_the_model_emits_no_tool_call() -> None:
    model = MockModel([LLMResponse(content="I would rather chat", tool_calls=[])])

    with pytest.raises(arcrun.StructuredCallError):
        await arcrun.run_structured(model, _messages(), tool=_tool())


async def test_a_hung_provider_raises_timeout_not_a_hang() -> None:
    class _Hang:
        async def invoke(self, *a: object, **k: object) -> object:
            await asyncio.sleep(10)
            raise AssertionError("unreachable")

    with pytest.raises(asyncio.TimeoutError):
        await arcrun.run_structured(_Hang(), _messages(), tool=_tool(), timeout=0.01)
