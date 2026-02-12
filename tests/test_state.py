"""Tests for RunState."""
import asyncio

import pytest

from arcrun.events import EventBus
from arcrun.registry import ToolRegistry


class TestRunState:
    def test_construction(self):
        from arcrun.state import RunState

        bus = EventBus(run_id="r")
        reg = ToolRegistry(tools=[], event_bus=bus)
        state = RunState(messages=[], registry=reg, event_bus=bus, run_id="run-1")
        assert state.run_id == "run-1"
        assert state.messages == []
        assert state.registry is reg
        assert state.event_bus is bus

    def test_defaults(self):
        from arcrun.state import RunState

        bus = EventBus(run_id="r")
        reg = ToolRegistry(tools=[], event_bus=bus)
        state = RunState(messages=[], registry=reg, event_bus=bus)
        assert state.turn_count == 0
        assert state.tokens_used == {"input": 0, "output": 0, "total": 0}
        assert state.cost_usd == 0.0
        assert state.tool_calls_made == 0
        assert state.run_id == ""
        assert state.transform_context is None

    def test_steer_queue_works(self):
        from arcrun.state import RunState

        bus = EventBus(run_id="r")
        reg = ToolRegistry(tools=[], event_bus=bus)
        state = RunState(messages=[], registry=reg, event_bus=bus)
        state.steer_queue.put_nowait("go left")
        assert not state.steer_queue.empty()
        assert state.steer_queue.get_nowait() == "go left"

    def test_followup_queue_works(self):
        from arcrun.state import RunState

        bus = EventBus(run_id="r")
        reg = ToolRegistry(tools=[], event_bus=bus)
        state = RunState(messages=[], registry=reg, event_bus=bus)
        state.followup_queue.put_nowait("also do X")
        assert state.followup_queue.get_nowait() == "also do X"

    def test_cancel_event_works(self):
        from arcrun.state import RunState

        bus = EventBus(run_id="r")
        reg = ToolRegistry(tools=[], event_bus=bus)
        state = RunState(messages=[], registry=reg, event_bus=bus)
        assert not state.cancel_event.is_set()
        state.cancel_event.set()
        assert state.cancel_event.is_set()
