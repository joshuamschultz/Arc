"""The loop binds its run_id as the spool correlation id for the whole run.

arcrun owns the run_id; arcllm (deep in the model call) spools ``llm_call``
records without one. Wrapping strategy execution in ``request_context(run_id)``
lets those records inherit the run id, so a run's LLM calls join its tool/run
events on ``request_id`` in the observability plane.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import LLMResponse, MockModel

from arcrun import StaticProvider
from arcrun.types import Tool


def _tools() -> list[Tool]:
    async def _echo(params: dict, ctx: object) -> str:
        return "ok"

    return [
        Tool(
            name="echo",
            description="echo",
            input_schema={"type": "object", "properties": {}},
            execute=_echo,
        )
    ]


class _SpoolingModel(MockModel):
    """A model that, like arcllm's telemetry, spools an ``llm_call`` per call —
    with no request_id of its own — so we can assert it inherits the run's."""

    def __init__(self, responses: list[LLMResponse], spool_path: Path) -> None:
        super().__init__(responses)
        self._spool_path = spool_path

    async def invoke(self, messages: list, tools: list | None = None) -> LLMResponse:
        from arcstore.records import SpoolRecord
        from arcstore.spool import record

        record(SpoolRecord(kind="llm_call", actor_did="did:llm"), path=self._spool_path)
        return await super().invoke(messages, tools)


@pytest.mark.asyncio
async def test_caller_pinned_run_id_is_used_for_the_run() -> None:
    """A caller may pin the run_id (the task dispatcher does, to link a task to
    its run before the loop starts). Every event — and thus every spooled
    ``request_id`` the observability plane joins on — carries that id."""
    from arcrun.loop import run

    model = MockModel([LLMResponse(content="done", stop_reason="end_turn")])
    result = await run(
        model, StaticProvider(_tools()), "prompt", "task", run_id="pinned-run-id"
    )
    assert result.events, "the run should emit lifecycle events"
    assert all(e.run_id == "pinned-run-id" for e in result.events)


@pytest.mark.asyncio
async def test_run_correlates_llm_calls_to_run_id(tmp_path: Path) -> None:
    from arcstore.spool import read

    from arcrun.loop import run

    spool = tmp_path / "operational.jsonl"
    model = _SpoolingModel([LLMResponse(content="done", stop_reason="end_turn")], spool)
    result = await run(model, StaticProvider(_tools()), "prompt", "task")

    run_id = result.events[0].run_id
    recorded = list(read(spool))
    assert recorded, "model should have spooled an llm_call"
    assert all(r.request_id == run_id for r in recorded)


@pytest.mark.asyncio
async def test_run_async_correlates_llm_calls_to_run_id(tmp_path: Path) -> None:
    # run_async runs the strategy on a fresh asyncio.Task — verify the task
    # snapshots the correlation binding set at create_task time.
    from arcstore.spool import read

    from arcrun.loop import run_async

    spool = tmp_path / "operational.jsonl"
    model = _SpoolingModel([LLMResponse(content="done", stop_reason="end_turn")], spool)
    handle = await run_async(model, StaticProvider(_tools()), "prompt", "task")
    result = await handle.result()

    run_id = result.events[0].run_id
    recorded = list(read(spool))
    assert recorded
    assert all(r.request_id == run_id for r in recorded)
