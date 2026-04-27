"""Phase C: arctrust AuditEvent emission from run_stream().

run_stream() must emit stream lifecycle AuditEvents:
- stream.start: emitted when the stream is initiated.
- stream.end:   emitted when TurnEndEvent is yielded (normal completion).
- stream.error: emitted when the underlying run() raises.

The AuditEvent sink is injected via an optional ``audit_sink`` keyword arg
added to run_stream().  When omitted the function falls back to logger-only
(backwards compatible).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from arctrust import AuditEvent, NullSink

from arcrun.streams import TurnEndEvent, run_stream
from arcrun.types import LoopResult

# ---------------------------------------------------------------------------
# Capture sink
# ---------------------------------------------------------------------------


class CaptureSink:
    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self._events.append(event)

    @property
    def events(self) -> list[AuditEvent]:
        return list(self._events)

    def actions(self) -> list[str]:
        return [e.action for e in self._events]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_loop_result(content: str = "hello world") -> LoopResult:
    return LoopResult(
        content=content,
        turns=1,
        tool_calls_made=0,
        cost_usd=0.0,
        tokens_used={"input": 5, "output": 3, "total": 8},
        strategy_used="react",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStreamAuditEvents:
    """run_stream() emits AuditEvents for stream lifecycle."""

    @pytest.mark.asyncio
    async def test_stream_start_emitted(self) -> None:
        """stream.start AuditEvent is emitted when the stream begins."""
        sink = CaptureSink()

        with patch("arcrun.loop.run", new=AsyncMock(return_value=_fake_loop_result())):
            stream = await run_stream(
                model=object(),
                tools=[],
                system_prompt="sys",
                task="task",
                audit_sink=sink,
            )
            async for _ in stream:
                pass

        assert "stream.start" in sink.actions(), f"Got: {sink.actions()}"

    @pytest.mark.asyncio
    async def test_stream_end_emitted(self) -> None:
        """stream.end AuditEvent is emitted when TurnEndEvent is yielded."""
        sink = CaptureSink()

        with patch("arcrun.loop.run", new=AsyncMock(return_value=_fake_loop_result())):
            stream = await run_stream(
                model=object(),
                tools=[],
                system_prompt="sys",
                task="task",
                audit_sink=sink,
            )
            async for _ in stream:
                pass

        assert "stream.end" in sink.actions(), f"Got: {sink.actions()}"

    @pytest.mark.asyncio
    async def test_stream_end_outcome_is_success_on_normal_completion(self) -> None:
        """stream.end AuditEvent has outcome='success' on normal completion."""
        sink = CaptureSink()

        with patch("arcrun.loop.run", new=AsyncMock(return_value=_fake_loop_result())):
            stream = await run_stream(
                model=object(),
                tools=[],
                system_prompt="sys",
                task="task",
                audit_sink=sink,
            )
            async for _ in stream:
                pass

        end_events = [e for e in sink.events if e.action == "stream.end"]
        assert len(end_events) == 1
        assert end_events[0].outcome == "success"

    @pytest.mark.asyncio
    async def test_null_sink_accepted(self) -> None:
        """NullSink is accepted as audit_sink — no error."""
        with patch("arcrun.loop.run", new=AsyncMock(return_value=_fake_loop_result())):
            stream = await run_stream(
                model=object(),
                tools=[],
                system_prompt="sys",
                task="task",
                audit_sink=NullSink(),
            )
            events = []
            async for ev in stream:
                events.append(ev)

        assert any(isinstance(e, TurnEndEvent) for e in events)

    @pytest.mark.asyncio
    async def test_no_sink_does_not_raise(self) -> None:
        """When audit_sink is omitted the stream works without error."""
        with patch("arcrun.loop.run", new=AsyncMock(return_value=_fake_loop_result())):
            stream = await run_stream(
                model=object(),
                tools=[],
                system_prompt="sys",
                task="task",
            )
            events = []
            async for ev in stream:
                events.append(ev)

        assert any(isinstance(e, TurnEndEvent) for e in events)

    @pytest.mark.asyncio
    async def test_stream_start_event_has_task_in_target(self) -> None:
        """stream.start event carries the task identifier in target field."""
        sink = CaptureSink()

        with patch("arcrun.loop.run", new=AsyncMock(return_value=_fake_loop_result())):
            stream = await run_stream(
                model=object(),
                tools=[],
                system_prompt="sys",
                task="the specific task",
                audit_sink=sink,
            )
            async for _ in stream:
                pass

        start_events = [e for e in sink.events if e.action == "stream.start"]
        assert len(start_events) == 1
        # Target should identify this stream run
        assert start_events[0].target != ""
