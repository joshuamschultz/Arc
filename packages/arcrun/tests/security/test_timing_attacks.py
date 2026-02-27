"""Adversarial: Timing and concurrency attacks (OWASP ASI08).

Tests that concurrent spawns don't deadlock, interleave events,
or corrupt state.
"""

from __future__ import annotations

import asyncio

import pytest

from arcrun.events import EventBus, verify_chain
from arcrun.types import Tool
from security.conftest import LLMResponse, MockModel


async def _noop(params: dict, ctx: object) -> str:
    return "ok"


def _make_tool() -> Tool:
    return Tool(
        name="echo",
        description="Echo",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
        execute=_noop,
    )


class TestTimingAttacks:
    @pytest.mark.asyncio
    async def test_parallel_runs_no_deadlock(self):
        """10 parallel run() calls complete without deadlock."""
        from arcrun.loop import run

        async def single_run(i: int):
            model = MockModel([LLMResponse(content=f"Run {i}", stop_reason="end_turn")])
            return await run(model, [_make_tool()], "prompt", f"Task {i}")

        results = await asyncio.gather(*[single_run(i) for i in range(10)])
        assert len(results) == 10
        for r in results:
            assert r.content is not None

    @pytest.mark.asyncio
    async def test_parallel_runs_unique_run_ids(self):
        """Each parallel run gets a unique run_id."""
        from arcrun.loop import run

        async def single_run(i: int):
            model = MockModel([LLMResponse(content=f"Run {i}", stop_reason="end_turn")])
            return await run(model, [_make_tool()], "prompt", f"Task {i}")

        results = await asyncio.gather(*[single_run(i) for i in range(10)])
        run_ids = set()
        for r in results:
            for e in r.events:
                run_ids.add(e.run_id)
        # Each run should have its own run_id
        assert len(run_ids) == 10

    @pytest.mark.asyncio
    async def test_parallel_runs_no_event_interleaving(self):
        """Events within each run form a valid chain."""
        from arcrun.loop import run

        async def single_run(i: int):
            model = MockModel([LLMResponse(content=f"Run {i}", stop_reason="end_turn")])
            return await run(model, [_make_tool()], "prompt", f"Task {i}")

        results = await asyncio.gather(*[single_run(i) for i in range(10)])
        for r in results:
            verification = r.verify_integrity()
            assert verification.valid, f"Chain broken for run: {verification.error}"

    @pytest.mark.asyncio
    async def test_concurrent_event_emission_thread_safe(self):
        """EventBus handles concurrent emission from threads correctly."""
        import threading

        bus = EventBus(run_id="concurrent-test")
        errors = []

        def emit_batch(prefix: str, count: int):
            try:
                for i in range(count):
                    bus.emit(f"{prefix}.{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=emit_batch, args=(f"t{i}", 50)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(bus.events) == 500
        result = verify_chain(bus.events)
        assert result.valid

    @pytest.mark.asyncio
    async def test_cancel_during_tool_execution(self):
        """Cancelling a run during tool execution is handled gracefully."""
        from arcrun.loop import run_async
        from security.conftest import ToolCall as _TC

        # Simulate a slow tool
        async def slow_tool(params: dict, ctx: object) -> str:
            await asyncio.sleep(10)
            return "done"

        tool = Tool(
            name="slow",
            description="Slow",
            input_schema={"type": "object", "properties": {}},
            execute=slow_tool,
        )

        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[_TC(id="tc1", name="slow", arguments={})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="Done.", stop_reason="end_turn"),
            ]
        )

        handle = await run_async(model, [tool], "prompt", "task")
        await asyncio.sleep(0.1)  # Let run start
        await handle.cancel()

        result = await handle.result()
        # Run should complete (cancelled or partial)
        assert result is not None
