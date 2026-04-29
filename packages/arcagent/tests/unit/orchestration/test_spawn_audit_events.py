"""Phase C: arctrust AuditEvent emission from spawn and streams.

spawn() must emit AuditEvents (via arctrust.audit.emit) in addition to the
existing EventBus events.  We test the two surfaces independently:
- spawn() emits spawn.start and spawn.complete AuditEvents.
- streams.run_stream() emits stream.start and stream.end AuditEvents.

The AuditEvent sink is injected via an optional ``audit_sink`` keyword arg
added to spawn() and run_stream().  When omitted the functions fall back to
logger-only (backwards compatible).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.state import RunState
from arcrun.types import LoopResult
from arctrust import AuditEvent, NullSink

from arcagent.orchestration.spawn import SpawnResult, spawn

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


def _make_state(depth: int = 0, max_depth: int = 5) -> RunState:
    bus = EventBus(run_id="test-spawn-audit")
    registry = ToolRegistry(tools=[], event_bus=bus)
    return RunState(
        messages=[],
        registry=registry,
        event_bus=bus,
        depth=depth,
        max_depth=max_depth,
    )


def _fake_loop_result() -> LoopResult:
    return LoopResult(
        content="done",
        turns=1,
        tool_calls_made=0,
        cost_usd=0.0,
        tokens_used={"input": 10, "output": 5, "total": 15},
        strategy_used="react",
    )


# ---------------------------------------------------------------------------
# spawn() AuditEvent tests
# ---------------------------------------------------------------------------


class TestSpawnAuditEvents:
    """spawn() emits AuditEvents for start and completion."""

    @pytest.mark.asyncio
    async def test_spawn_start_event_emitted(self) -> None:
        """AuditEvent with action 'spawn.start' is emitted at the beginning."""
        sink = CaptureSink()
        fake_result = _fake_loop_result()

        with patch("arcrun.loop.run", new=AsyncMock(return_value=fake_result)):
            await spawn(
                parent_state=_make_state(),
                task="test task",
                tools=[],
                system_prompt="sys",
                model=MagicMock(),
                audit_sink=sink,
            )

        assert "spawn.start" in sink.actions(), f"Expected spawn.start, got {sink.actions()}"

    @pytest.mark.asyncio
    async def test_spawn_complete_event_emitted_on_success(self) -> None:
        """AuditEvent with action 'spawn.complete' is emitted on successful completion."""
        sink = CaptureSink()
        fake_result = _fake_loop_result()

        with patch("arcrun.loop.run", new=AsyncMock(return_value=fake_result)):
            await spawn(
                parent_state=_make_state(),
                task="test task",
                tools=[],
                system_prompt="sys",
                model=MagicMock(),
                audit_sink=sink,
            )

        assert "spawn.complete" in sink.actions(), f"Expected spawn.complete, got {sink.actions()}"

    @pytest.mark.asyncio
    async def test_spawn_complete_event_emitted_on_timeout(self) -> None:
        """AuditEvent 'spawn.complete' is emitted even when the child times out."""
        sink = CaptureSink()

        async def _slow(*args: Any, **kwargs: Any) -> LoopResult:
            await asyncio.sleep(9999)
            return _fake_loop_result()

        with patch("arcrun.loop.run", new=_slow):
            result = await spawn(
                parent_state=_make_state(),
                task="slow task",
                tools=[],
                system_prompt="sys",
                model=MagicMock(),
                wallclock_timeout_s=0.01,
                audit_sink=sink,
            )

        assert result.status == "timeout"
        assert "spawn.complete" in sink.actions()

    @pytest.mark.asyncio
    async def test_spawn_complete_event_emitted_on_error(self) -> None:
        """AuditEvent 'spawn.complete' is emitted when child raises."""
        sink = CaptureSink()

        with patch(
            "arcrun.loop.run",
            new=AsyncMock(side_effect=RuntimeError("child blew up")),
        ):
            result = await spawn(
                parent_state=_make_state(),
                task="failing task",
                tools=[],
                system_prompt="sys",
                model=MagicMock(),
                audit_sink=sink,
            )

        assert result.status == "error"
        assert "spawn.complete" in sink.actions()

    @pytest.mark.asyncio
    async def test_spawn_start_event_has_child_did(self) -> None:
        """spawn.start AuditEvent target carries child DID."""
        from arctrust import ChildIdentity

        sink = CaptureSink()
        identity = ChildIdentity(
            did="did:arc:delegate:child/testaudit",
            sk_bytes=b"\x01" * 32,
            ttl_s=300,
        )
        fake_result = _fake_loop_result()

        with patch("arcrun.loop.run", new=AsyncMock(return_value=fake_result)):
            await spawn(
                parent_state=_make_state(),
                task="task",
                tools=[],
                system_prompt="sys",
                model=MagicMock(),
                identity=identity,
                audit_sink=sink,
            )

        start_events = [e for e in sink.events if e.action == "spawn.start"]
        assert len(start_events) >= 1
        assert start_events[0].target == "did:arc:delegate:child/testaudit"

    @pytest.mark.asyncio
    async def test_depth_exceeded_emits_spawn_denied(self) -> None:
        """When depth is at max, a spawn.denied AuditEvent is emitted."""
        sink = CaptureSink()
        state = _make_state(depth=5, max_depth=5)  # Already at limit

        result = await spawn(
            parent_state=state,
            task="task",
            tools=[],
            system_prompt="sys",
            model=MagicMock(),
            audit_sink=sink,
        )

        assert result.status == "error"
        # Must emit some audit event — either spawn.denied or spawn.complete(error)
        assert len(sink.events) >= 1
        action = sink.events[0].action
        assert action in ("spawn.denied", "spawn.complete"), f"Unexpected action: {action}"

    @pytest.mark.asyncio
    async def test_null_sink_accepted(self) -> None:
        """NullSink is accepted as audit_sink — no error."""
        fake_result = _fake_loop_result()

        with patch("arcrun.loop.run", new=AsyncMock(return_value=fake_result)):
            result = await spawn(
                parent_state=_make_state(),
                task="task",
                tools=[],
                system_prompt="sys",
                model=MagicMock(),
                audit_sink=NullSink(),
            )
        assert isinstance(result, SpawnResult)

    @pytest.mark.asyncio
    async def test_no_sink_does_not_raise(self) -> None:
        """When audit_sink is omitted the function works without error."""
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
