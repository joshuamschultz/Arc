"""Phase D: UIReporter callback hook in run_stream() and spawn().

Verifies that when a ui_reporter is injected, emit_run_event() is called
with the expected event_types for stream and spawn lifecycle events.

Design:
- FakeReporter records all emit_run_event calls (event_type + data).
- No arcui import — duck typing only, preserving layer purity.
- run_stream: expects stream_start, tool_start, tool_end, stream_token,
  stream_end on a normal run.
- spawn: expects spawn_start, spawn_complete on success; spawn_denied
  on depth exhaustion.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.orchestration.spawn import spawn
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.state import RunState
from arcrun.streams import TurnEndEvent, run_stream
from arcrun.types import LoopResult

# ---------------------------------------------------------------------------
# FakeReporter — records emit_run_event calls (no arcui dependency)
# ---------------------------------------------------------------------------


class FakeReporter:
    """Minimal duck-typed reporter that records calls to emit_run_event."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def emit_run_event(self, *, event_type: str, data: dict[str, Any]) -> None:
        self.calls.append({"event_type": event_type, "data": data})

    def event_types(self) -> list[str]:
        return [c["event_type"] for c in self.calls]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_loop_result(content: str = "hello world") -> LoopResult:
    return LoopResult(
        content=content,
        turns=2,
        tool_calls_made=0,
        cost_usd=0.01,
        tokens_used={"input": 10, "output": 8, "total": 18},
        strategy_used="react",
    )


def _make_state(depth: int = 0, max_depth: int = 5) -> RunState:
    bus = EventBus(run_id="test-ui-reporter")
    registry = ToolRegistry(tools=[], event_bus=bus)
    return RunState(
        messages=[],
        registry=registry,
        event_bus=bus,
        depth=depth,
        max_depth=max_depth,
    )


# ---------------------------------------------------------------------------
# run_stream() UIReporter tests
# ---------------------------------------------------------------------------


class TestRunStreamUIReporter:
    """run_stream() calls emit_run_event on ui_reporter for stream lifecycle."""

    @pytest.mark.asyncio
    async def test_stream_start_emitted(self) -> None:
        """emit_run_event called with event_type='stream_start' when stream begins."""
        reporter = FakeReporter()

        with patch("arcrun.loop.run", new=AsyncMock(return_value=_fake_loop_result())):
            stream = await run_stream(
                model=object(),
                tools=[],
                system_prompt="sys",
                task="task",
                ui_reporter=reporter,
            )
            async for _ in stream:
                pass

        assert "stream_start" in reporter.event_types(), (
            f"Expected stream_start; got {reporter.event_types()}"
        )

    @pytest.mark.asyncio
    async def test_stream_end_emitted(self) -> None:
        """emit_run_event called with event_type='stream_end' after TurnEndEvent."""
        reporter = FakeReporter()

        with patch("arcrun.loop.run", new=AsyncMock(return_value=_fake_loop_result())):
            stream = await run_stream(
                model=object(),
                tools=[],
                system_prompt="sys",
                task="task",
                ui_reporter=reporter,
            )
            async for _ in stream:
                pass

        assert "stream_end" in reporter.event_types(), (
            f"Expected stream_end; got {reporter.event_types()}"
        )

    @pytest.mark.asyncio
    async def test_stream_token_emitted_per_word(self) -> None:
        """emit_run_event called with event_type='stream_token' for each content word."""
        reporter = FakeReporter()

        with patch(
            "arcrun.loop.run",
            new=AsyncMock(return_value=_fake_loop_result("hello world")),
        ):
            stream = await run_stream(
                model=object(),
                tools=[],
                system_prompt="sys",
                task="task",
                ui_reporter=reporter,
            )
            async for _ in stream:
                pass

        token_calls = [c for c in reporter.calls if c["event_type"] == "stream_token"]
        assert len(token_calls) >= 1, "Expected at least one stream_token call"
        # Each token call should carry the text chunk
        for call in token_calls:
            assert "text" in call["data"], f"stream_token data missing 'text': {call['data']}"

    @pytest.mark.asyncio
    async def test_turn_end_data_in_stream_end(self) -> None:
        """stream_end event carries turns and tool_calls_made from LoopResult."""
        reporter = FakeReporter()
        result = _fake_loop_result()

        with patch("arcrun.loop.run", new=AsyncMock(return_value=result)):
            stream = await run_stream(
                model=object(),
                tools=[],
                system_prompt="sys",
                task="task",
                ui_reporter=reporter,
            )
            async for _ in stream:
                pass

        end_calls = [c for c in reporter.calls if c["event_type"] == "stream_end"]
        assert len(end_calls) == 1
        data = end_calls[0]["data"]
        assert data.get("turns") == result.turns
        assert data.get("tool_calls_made") == result.tool_calls_made

    @pytest.mark.asyncio
    async def test_no_reporter_does_not_raise(self) -> None:
        """When ui_reporter is omitted (default None), the stream works without error."""
        with patch("arcrun.loop.run", new=AsyncMock(return_value=_fake_loop_result())):
            stream = await run_stream(
                model=object(),
                tools=[],
                system_prompt="sys",
                task="task",
            )
            events = [ev async for ev in stream]

        assert any(isinstance(e, TurnEndEvent) for e in events)

    @pytest.mark.asyncio
    async def test_tool_start_emitted(self) -> None:
        """emit_run_event called with event_type='tool_start' when EventBus gets tool.start."""
        reporter = FakeReporter()

        async def _run_with_tool_event(*args: Any, **kwargs: Any) -> LoopResult:
            # Simulate a tool.start event being emitted during the run via EventBus
            on_event = kwargs.get("on_event")
            if on_event is not None:
                from arcrun.events import EventBus
                bus = EventBus(run_id="test-tool-start", on_event=on_event)
                bus.emit("tool.start", {"name": "read_file", "arguments": {}})
            return _fake_loop_result()

        with patch("arcrun.loop.run", new=_run_with_tool_event):
            stream = await run_stream(
                model=object(),
                tools=[],
                system_prompt="sys",
                task="task",
                ui_reporter=reporter,
            )
            async for _ in stream:
                pass

        assert "tool_start" in reporter.event_types(), (
            f"Expected tool_start; got {reporter.event_types()}"
        )

    @pytest.mark.asyncio
    async def test_tool_end_emitted(self) -> None:
        """emit_run_event called with event_type='tool_end' when EventBus gets tool.end."""
        reporter = FakeReporter()

        async def _run_with_tool_events(*args: Any, **kwargs: Any) -> LoopResult:
            on_event = kwargs.get("on_event")
            if on_event is not None:
                from arcrun.events import EventBus
                bus = EventBus(run_id="test-tool-end", on_event=on_event)
                bus.emit("tool.start", {"name": "read_file", "arguments": {}})
                bus.emit("tool.end", {"result": "file contents"})
            return _fake_loop_result()

        with patch("arcrun.loop.run", new=_run_with_tool_events):
            stream = await run_stream(
                model=object(),
                tools=[],
                system_prompt="sys",
                task="task",
                ui_reporter=reporter,
            )
            async for _ in stream:
                pass

        assert "tool_end" in reporter.event_types(), (
            f"Expected tool_end; got {reporter.event_types()}"
        )


# ---------------------------------------------------------------------------
# spawn() UIReporter tests
# ---------------------------------------------------------------------------


class TestSpawnUIReporter:
    """spawn() calls emit_run_event on ui_reporter for spawn lifecycle."""

    @pytest.mark.asyncio
    async def test_spawn_start_emitted(self) -> None:
        """emit_run_event called with event_type='spawn_start' at spawn begin."""
        reporter = FakeReporter()
        fake_result = _fake_loop_result()

        with patch("arcrun.loop.run", new=AsyncMock(return_value=fake_result)):
            await spawn(
                parent_state=_make_state(),
                task="test task",
                tools=[],
                system_prompt="sys",
                model=MagicMock(),
                ui_reporter=reporter,
            )

        assert "spawn_start" in reporter.event_types(), (
            f"Expected spawn_start; got {reporter.event_types()}"
        )

    @pytest.mark.asyncio
    async def test_spawn_complete_emitted_on_success(self) -> None:
        """emit_run_event called with event_type='spawn_complete' after success."""
        reporter = FakeReporter()
        fake_result = _fake_loop_result()

        with patch("arcrun.loop.run", new=AsyncMock(return_value=fake_result)):
            await spawn(
                parent_state=_make_state(),
                task="test task",
                tools=[],
                system_prompt="sys",
                model=MagicMock(),
                ui_reporter=reporter,
            )

        assert "spawn_complete" in reporter.event_types(), (
            f"Expected spawn_complete; got {reporter.event_types()}"
        )

    @pytest.mark.asyncio
    async def test_spawn_start_data_has_child_run_id(self) -> None:
        """spawn_start event data carries child_run_id for correlation."""
        reporter = FakeReporter()
        fake_result = _fake_loop_result()

        with patch("arcrun.loop.run", new=AsyncMock(return_value=fake_result)):
            await spawn(
                parent_state=_make_state(),
                task="test task",
                tools=[],
                system_prompt="sys",
                model=MagicMock(),
                ui_reporter=reporter,
            )

        start_calls = [c for c in reporter.calls if c["event_type"] == "spawn_start"]
        assert len(start_calls) == 1
        assert "child_run_id" in start_calls[0]["data"], (
            f"spawn_start data missing child_run_id: {start_calls[0]['data']}"
        )

    @pytest.mark.asyncio
    async def test_spawn_denied_emitted_on_depth_exhaustion(self) -> None:
        """emit_run_event called with event_type='spawn_denied' when depth is maxed."""
        reporter = FakeReporter()
        state = _make_state(depth=5, max_depth=5)

        await spawn(
            parent_state=state,
            task="task",
            tools=[],
            system_prompt="sys",
            model=MagicMock(),
            ui_reporter=reporter,
        )

        assert "spawn_denied" in reporter.event_types(), (
            f"Expected spawn_denied; got {reporter.event_types()}"
        )

    @pytest.mark.asyncio
    async def test_no_reporter_does_not_raise(self) -> None:
        """When ui_reporter is omitted (default None), spawn works without error."""
        from arcagent.orchestration.spawn import SpawnResult

        fake_result = _fake_loop_result()

        with patch("arcrun.loop.run", new=AsyncMock(return_value=fake_result)):
            result = await spawn(
                parent_state=_make_state(),
                task="task",
                tools=[],
                system_prompt="sys",
                model=MagicMock(),
            )

        assert isinstance(result, SpawnResult)

    @pytest.mark.asyncio
    async def test_spawn_complete_data_has_status(self) -> None:
        """spawn_complete event data carries status field."""
        reporter = FakeReporter()
        fake_result = _fake_loop_result()

        with patch("arcrun.loop.run", new=AsyncMock(return_value=fake_result)):
            await spawn(
                parent_state=_make_state(),
                task="test task",
                tools=[],
                system_prompt="sys",
                model=MagicMock(),
                ui_reporter=reporter,
            )

        complete_calls = [c for c in reporter.calls if c["event_type"] == "spawn_complete"]
        assert len(complete_calls) == 1
        assert "status" in complete_calls[0]["data"], (
            f"spawn_complete data missing status: {complete_calls[0]['data']}"
        )
