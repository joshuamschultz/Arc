"""Tests for event system."""
import pytest


class TestEvent:
    def test_construction(self):
        from arcrun.events import Event

        e = Event(type="tool.start", timestamp=1.0, run_id="run-1", data={"name": "search"})
        assert e.type == "tool.start"
        assert e.timestamp == 1.0
        assert e.run_id == "run-1"
        assert e.data["name"] == "search"

    def test_data_defaults_to_dict(self):
        from arcrun.events import Event

        e = Event(type="test", timestamp=0.0, run_id="r", data={})
        assert isinstance(e.data, dict)


class TestEventBus:
    def test_emit_creates_event(self):
        from arcrun.events import EventBus

        bus = EventBus(run_id="run-1")
        event = bus.emit("loop.start", {"task": "test"})
        assert event.type == "loop.start"
        assert event.run_id == "run-1"
        assert event.data["task"] == "test"
        assert event.timestamp > 0

    def test_emit_collects_events(self):
        from arcrun.events import EventBus

        bus = EventBus(run_id="run-1")
        bus.emit("a")
        bus.emit("b")
        bus.emit("c")
        assert len(bus.events) == 3
        assert [e.type for e in bus.events] == ["a", "b", "c"]

    def test_emit_with_no_data(self):
        from arcrun.events import EventBus

        bus = EventBus(run_id="r")
        event = bus.emit("test")
        assert event.data == {}

    def test_on_event_callback_called(self):
        from arcrun.events import EventBus

        received = []
        bus = EventBus(run_id="r", on_event=lambda e: received.append(e))
        bus.emit("x", {"val": 1})
        assert len(received) == 1
        assert received[0].type == "x"

    def test_on_event_callback_exception_propagates(self):
        from arcrun.events import EventBus

        def bad_handler(e):
            raise ValueError("handler error")

        bus = EventBus(run_id="r", on_event=bad_handler)
        with pytest.raises(ValueError, match="handler error"):
            bus.emit("test")

    def test_events_property_returns_copy(self):
        from arcrun.events import EventBus

        bus = EventBus(run_id="r")
        bus.emit("a")
        events = bus.events
        events.clear()
        assert len(bus.events) == 1
