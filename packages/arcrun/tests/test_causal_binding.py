"""arcrun refines run / turn / llm_call / tool_call ids onto the one causal context.

There is a single source for "which run am I in": ``arctrust.causal``. The
retired parallel contextvars (arcstore ``_request_id_var``, arcrun
``_current_run_id``) must not exist, and background work started during a run
must never inherit that run's ids.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from arctrust import causal
from packages.arcrun.tests.conftest import LLMResponse, MockModel

import arcrun
from arcrun.types import Tool


def _provider(execute: Any) -> arcrun.StaticProvider:
    return arcrun.StaticProvider(
        [
            Tool(
                name="echo",
                description="echo",
                input_schema={"type": "object", "properties": {}},
                execute=execute,
            )
        ]
    )


def _echo_provider(seen: list[causal.CausalContext | None]) -> arcrun.StaticProvider:
    async def _echo(_params: dict[str, Any], _ctx: object) -> str:
        seen.append(causal.current())
        return "ok"

    return _provider(_echo)


class _ObservingModel(MockModel):
    def __init__(self, responses: list[LLMResponse], seen: list[causal.CausalContext | None]):
        super().__init__(responses)
        self._seen = seen

    async def invoke(self, messages: list, tools: list | None = None, **kw: Any) -> LLMResponse:
        self._seen.append(causal.current())
        return await super().invoke(messages, tools)


def _tool_response() -> LLMResponse:
    from arcllm import ToolCall

    return LLMResponse(
        content="",
        stop_reason="tool_use",
        tool_calls=[ToolCall(id="tc-1", name="echo", arguments={})],
    )


def test_parallel_contextvars_are_gone() -> None:
    import arcstore.spool as spool

    import arcrun.loop as loop

    assert not hasattr(loop, "_current_run_id")
    assert not hasattr(spool, "_request_id_var")
    assert not hasattr(spool, "request_context")


@pytest.mark.asyncio
async def test_every_llm_call_and_tool_step_carries_distinct_causal_ids() -> None:
    llm_seen: list[causal.CausalContext | None] = []
    tool_seen: list[causal.CausalContext | None] = []
    model = _ObservingModel(
        [_tool_response(), _tool_response(), LLMResponse(content="done", stop_reason="end_turn")],
        llm_seen,
    )
    root = causal.root("agent", "did:arc:agent:a")
    with causal.bind(root):
        result = await arcrun.run(
            model, _echo_provider(tool_seen), "sys", "task", allowed_strategies=["react"]
        )
        # Scopes unwind: nothing leaks out of the run into the caller.
        after = causal.current()
        assert after is not None and after.run_id is None and after.llm_call_id is None
    run_id = result.events[0].run_id
    assert len(llm_seen) == 3 and len(tool_seen) == 2
    for ctx in (*llm_seen, *tool_seen):
        assert ctx is not None
        assert ctx.run_id == run_id
        assert ctx.initiator_id == "did:arc:agent:a"
        assert ctx.request_id == root.request_id
    assert len({c.llm_call_id for c in llm_seen if c}) == 3
    assert all(c.tool_call_id is None for c in llm_seen if c)
    assert {c.tool_call_id for c in tool_seen if c} == {"tc-1"}
    assert [c.turn for c in llm_seen if c] == [0, 1, 2]


@pytest.mark.asyncio
async def test_a_tool_step_names_the_model_call_that_asked_for_it() -> None:
    """The tool call's policy row must say which LLM call requested it (P20-7).

    The model call's scope closes before its tool calls dispatch, so without
    carrying the id forward every tool record lost its ``llm_call_id``.
    """
    llm_seen: list[causal.CausalContext | None] = []
    tool_seen: list[causal.CausalContext | None] = []
    model = _ObservingModel(
        [_tool_response(), _tool_response(), LLMResponse(content="done", stop_reason="end_turn")],
        llm_seen,
    )
    with causal.bind(causal.root("agent", "did:arc:agent:a")):
        await arcrun.run(
            model, _echo_provider(tool_seen), "sys", "task", allowed_strategies=["react"]
        )
        after = causal.current()
        assert after is not None and after.llm_call_id is None
    asked = [c.llm_call_id for c in llm_seen[:2] if c]
    assert len(asked) == 2 and all(asked)
    assert [c.llm_call_id for c in tool_seen if c] == asked


@pytest.mark.asyncio
async def test_background_task_spawned_mid_run_never_inherits_run_ids() -> None:
    """Forced interleaving: the detached task is alive WHILE the run is mid-tool."""
    started = asyncio.Event()
    release = asyncio.Event()
    background: list[causal.CausalContext | None] = []
    in_tool: list[causal.CausalContext | None] = []

    async def _background() -> None:
        started.set()
        await release.wait()  # still running after the tool step has returned
        background.append(causal.current())

    async def _spawning_tool(_params: dict[str, Any], _ctx: object) -> str:
        in_tool.append(causal.current())
        causal.spawn_detached(_background(), initiator_id="did:arc:system:bg")
        await started.wait()
        return "ok"

    model = MockModel([_tool_response(), LLMResponse(content="done", stop_reason="end_turn")])
    with causal.bind(causal.root("agent", "did:arc:agent:a")):
        await arcrun.run(
            model, _provider(_spawning_tool), "sys", "task", allowed_strategies=["react"]
        )
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    (tool_ctx,) = in_tool
    (bg,) = background
    assert tool_ctx is not None and tool_ctx.run_id and tool_ctx.tool_call_id == "tc-1"
    assert bg is not None
    assert bg.run_id is None and bg.tool_call_id is None and bg.llm_call_id is None
    assert bg.request_id != tool_ctx.request_id
    assert bg.initiator_id == "did:arc:system:bg"


@pytest.mark.asyncio
async def test_run_with_no_bound_root_is_unattributed_not_borrowed() -> None:
    seen: list[causal.CausalContext | None] = []
    model = MockModel([_tool_response(), LLMResponse(content="done", stop_reason="end_turn")])
    await arcrun.run(model, _echo_provider(seen), "sys", "task", allowed_strategies=["react"])
    (ctx,) = seen
    assert ctx is not None
    assert ctx.initiator_id == causal.UNATTRIBUTED and ctx.run_id
    assert causal.current() is None
