"""Tests for RunState."""

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
        from arcrun.state import Injection, RunState

        bus = EventBus(run_id="r")
        reg = ToolRegistry(tools=[], event_bus=bus)
        state = RunState(messages=[], registry=reg, event_bus=bus)
        injection = Injection.new("did:arc:caller", "go left")
        state.steer_queue.put_nowait(injection)
        assert not state.steer_queue.empty()
        drained = state.steer_queue.get_nowait()
        assert drained.message == "go left"
        assert drained.caller_did == "did:arc:caller"

    def test_followup_queue_works(self):
        from arcrun.state import Injection, RunState

        bus = EventBus(run_id="r")
        reg = ToolRegistry(tools=[], event_bus=bus)
        state = RunState(messages=[], registry=reg, event_bus=bus)
        state.followup_queue.put_nowait(Injection.new("did:arc:caller", "also do X"))
        assert state.followup_queue.get_nowait().message == "also do X"

    def test_cancel_event_works(self):
        from arcrun.state import RunState

        bus = EventBus(run_id="r")
        reg = ToolRegistry(tools=[], event_bus=bus)
        state = RunState(messages=[], registry=reg, event_bus=bus)
        assert not state.cancel_event.is_set()
        state.cancel_event.set()
        assert state.cancel_event.is_set()


class TestInjection:
    def test_new_generates_unique_message_id(self):
        from arcrun.state import Injection

        first = Injection.new("did:arc:caller", "hi")
        second = Injection.new("did:arc:caller", "hi")
        assert first.message_id != second.message_id

    def test_new_rejects_empty_caller_did(self):
        import pytest

        from arcrun.state import Injection

        with pytest.raises(ValueError, match="caller_did"):
            Injection.new("", "hi")
