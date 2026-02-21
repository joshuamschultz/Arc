"""Tests for event system."""
import threading
from collections.abc import Mapping
from types import MappingProxyType

import pytest


class TestEvent:
    def test_construction(self):
        from arcrun.events import Event

        e = Event(type="tool.start", timestamp=1.0, run_id="run-1", data={"name": "search"})
        assert e.type == "tool.start"
        assert e.timestamp == 1.0
        assert e.run_id == "run-1"
        assert e.data["name"] == "search"

    def test_data_is_mapping(self):
        from arcrun.events import Event

        e = Event(type="test", timestamp=0.0, run_id="r", data={})
        assert isinstance(e.data, Mapping)

    def test_data_auto_converts_dict_to_mapping_proxy(self):
        """Dict passed to data= is auto-converted to MappingProxyType."""
        from arcrun.events import Event

        e = Event(type="test", timestamp=0.0, run_id="r", data={"k": "v"})
        assert isinstance(e.data, MappingProxyType)
        assert e.data["k"] == "v"


class TestEventImmutability:
    """A1: Event is frozen — no mutation allowed."""

    def test_frozen_type_field(self):
        from arcrun.events import Event

        e = Event(type="test", timestamp=0.0, run_id="r", data={})
        with pytest.raises(AttributeError):
            e.type = "changed"  # type: ignore[misc]

    def test_frozen_data_field(self):
        from arcrun.events import Event

        e = Event(type="test", timestamp=0.0, run_id="r", data={})
        with pytest.raises(AttributeError):
            e.data = {"new": "data"}  # type: ignore[misc]

    def test_data_dict_mutation_blocked(self):
        """MappingProxyType prevents key assignment."""
        from arcrun.events import Event

        e = Event(type="test", timestamp=0.0, run_id="r", data={"key": "val"})
        with pytest.raises(TypeError):
            e.data["key"] = "mutated"  # type: ignore[index]

    def test_data_supports_read_access(self):
        from arcrun.events import Event

        e = Event(type="test", timestamp=0.0, run_id="r", data={"a": 1, "b": 2})
        assert e.data["a"] == 1
        assert len(e.data) == 2
        assert list(e.data.keys()) == ["a", "b"]

    def test_hash_chain_fields_present(self):
        from arcrun.events import Event

        e = Event(type="test", timestamp=0.0, run_id="r", data={})
        assert hasattr(e, "sequence")
        assert hasattr(e, "prev_hash")
        assert hasattr(e, "event_hash")

    def test_hash_chain_defaults(self):
        """Direct construction gets default hash chain values."""
        from arcrun.events import Event

        e = Event(type="test", timestamp=0.0, run_id="r", data={})
        assert e.sequence == 0
        assert e.prev_hash == ""
        assert e.event_hash == ""


class TestHashChainComputation:
    """A2: Hash chain computation is correct."""

    def test_single_event_has_valid_self_hash(self):
        from arcrun.events import EventBus, _canonical_bytes, _compute_event_hash

        bus = EventBus(run_id="run-1")
        event = bus.emit("test", {"key": "val"})

        # Recompute hash and verify
        canonical = _canonical_bytes(
            event.type, event.timestamp, event.run_id, event.data, event.sequence
        )
        expected = _compute_event_hash(event.prev_hash, canonical)
        assert event.event_hash == expected
        assert len(event.event_hash) == 64  # SHA-256 hex digest

    def test_two_events_chain_correctly(self):
        from arcrun.events import EventBus

        bus = EventBus(run_id="run-1")
        e1 = bus.emit("first")
        e2 = bus.emit("second")

        assert e2.prev_hash == e1.event_hash
        assert e1.event_hash != e2.event_hash

    def test_genesis_event_has_genesis_prev_hash(self):
        from arcrun.events import GENESIS_PREV_HASH, EventBus

        bus = EventBus(run_id="run-1")
        event = bus.emit("genesis")

        assert event.prev_hash == GENESIS_PREV_HASH
        assert event.prev_hash == "0" * 64

    def test_canonical_bytes_deterministic(self):
        from arcrun.events import _canonical_bytes

        b1 = _canonical_bytes("test", 1.0, "run-1", {"a": 1}, 0)
        b2 = _canonical_bytes("test", 1.0, "run-1", {"a": 1}, 0)
        assert b1 == b2

    def test_canonical_bytes_sorted_keys(self):
        """Different key order produces same bytes."""
        from arcrun.events import _canonical_bytes
        from collections import OrderedDict

        b1 = _canonical_bytes("t", 0.0, "r", {"z": 1, "a": 2}, 0)
        b2 = _canonical_bytes("t", 0.0, "r", OrderedDict([("a", 2), ("z", 1)]), 0)
        assert b1 == b2

    def test_sequence_increments(self):
        from arcrun.events import EventBus

        bus = EventBus(run_id="run-1")
        e0 = bus.emit("a")
        e1 = bus.emit("b")
        e2 = bus.emit("c")
        assert e0.sequence == 0
        assert e1.sequence == 1
        assert e2.sequence == 2


class TestVerifyChain:
    """A3: Chain verification detects tampering."""

    def test_valid_chain(self):
        from arcrun.events import EventBus, verify_chain

        bus = EventBus(run_id="run-1")
        bus.emit("a", {"x": 1})
        bus.emit("b", {"y": 2})
        bus.emit("c")

        result = verify_chain(bus.events)
        assert result.valid is True
        assert result.event_count == 3
        assert result.first_broken_index is None
        assert result.error is None

    def test_empty_chain_is_valid(self):
        from arcrun.events import verify_chain

        result = verify_chain([])
        assert result.valid is True
        assert result.event_count == 0

    def test_single_event_chain_valid(self):
        from arcrun.events import EventBus, verify_chain

        bus = EventBus(run_id="r")
        bus.emit("only")
        result = verify_chain(bus.events)
        assert result.valid is True

    def test_modified_data_detected(self):
        """Tampering with event data breaks self-hash."""
        from arcrun.events import Event, EventBus, verify_chain

        bus = EventBus(run_id="run-1")
        bus.emit("a", {"secret": "original"})
        bus.emit("b")

        events = bus.events
        # Tamper: replace first event with modified data
        tampered = Event(
            type=events[0].type,
            timestamp=events[0].timestamp,
            run_id=events[0].run_id,
            data={"secret": "TAMPERED"},
            sequence=events[0].sequence,
            prev_hash=events[0].prev_hash,
            event_hash=events[0].event_hash,  # Keep old hash — will mismatch
        )
        events[0] = tampered

        result = verify_chain(events)
        assert result.valid is False
        assert result.first_broken_index == 0
        assert result.error == "self-hash mismatch"

    def test_deleted_event_detected(self):
        """Removing an event breaks chain linkage."""
        from arcrun.events import EventBus, verify_chain

        bus = EventBus(run_id="run-1")
        bus.emit("a")
        bus.emit("b")
        bus.emit("c")

        events = bus.events
        del events[1]  # Remove middle event

        result = verify_chain(events)
        assert result.valid is False
        # Either chain break or sequence gap
        assert result.error in ("chain break", "sequence gap")

    def test_inserted_event_detected(self):
        """Inserting a fabricated event breaks sequence."""
        from arcrun.events import Event, EventBus, verify_chain

        bus = EventBus(run_id="run-1")
        bus.emit("a")
        bus.emit("c")

        events = bus.events
        # Insert fabricated event between a and c
        fake = Event(
            type="fake",
            timestamp=0.0,
            run_id="run-1",
            data={},
            sequence=1,
            prev_hash=events[0].event_hash,
            event_hash="0" * 64,
        )
        events.insert(1, fake)

        result = verify_chain(events)
        assert result.valid is False

    def test_reordered_events_detected(self):
        """Swapping event order breaks chain."""
        from arcrun.events import EventBus, verify_chain

        bus = EventBus(run_id="run-1")
        bus.emit("first")
        bus.emit("second")
        bus.emit("third")

        events = bus.events
        events[0], events[1] = events[1], events[0]  # Swap first two

        result = verify_chain(events)
        assert result.valid is False


class TestChainVerificationResult:
    """A3: ChainVerificationResult dataclass."""

    def test_construction(self):
        from arcrun.events import ChainVerificationResult

        r = ChainVerificationResult(valid=True, event_count=5)
        assert r.valid is True
        assert r.event_count == 5
        assert r.first_broken_index is None
        assert r.error is None

    def test_failure_construction(self):
        from arcrun.events import ChainVerificationResult

        r = ChainVerificationResult(
            valid=False, event_count=3, first_broken_index=1, error="chain break"
        )
        assert r.valid is False
        assert r.first_broken_index == 1
        assert r.error == "chain break"


class TestThreadSafeEventBus:
    """A4: EventBus thread safety."""

    def test_lock_exists(self):
        from arcrun.events import EventBus

        bus = EventBus(run_id="r")
        assert hasattr(bus, "_lock")
        assert isinstance(bus._lock, threading.Lock)

    def test_concurrent_emit_produces_valid_chain(self):
        from arcrun.events import EventBus, verify_chain

        bus = EventBus(run_id="run-1")
        errors = []

        def emit_events(prefix: str, count: int) -> None:
            try:
                for i in range(count):
                    bus.emit(f"{prefix}.{i}", {"thread": prefix})
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=emit_events, args=(f"t{i}", 20))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(bus.events) == 100  # 5 threads * 20 events
        result = verify_chain(bus.events)
        assert result.valid is True

    def test_observer_error_doesnt_break_chain(self):
        from arcrun.events import EventBus, verify_chain

        call_count = 0

        def bad_observer(event):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ValueError("observer crash")

        bus = EventBus(run_id="r", on_event=bad_observer)
        bus.emit("a")
        bus.emit("b")  # Observer crashes here
        bus.emit("c")

        assert len(bus.events) == 3
        result = verify_chain(bus.events)
        assert result.valid is True


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

    def test_on_event_callback_exception_isolated(self):
        from arcrun.events import EventBus

        def bad_handler(e):
            raise ValueError("handler error")

        bus = EventBus(run_id="r", on_event=bad_handler)
        event = bus.emit("test")  # Should not raise
        assert event.type == "test"
        assert len(bus.events) == 1

    def test_on_event_callback_exception_is_logged(self, caplog):
        import logging

        from arcrun.events import EventBus

        def bad_handler(e):
            raise ValueError("handler error")

        bus = EventBus(run_id="r", on_event=bad_handler)
        with caplog.at_level(logging.WARNING, logger="arcrun.events"):
            bus.emit("test")

        assert "Observer callback failed" in caplog.text

    def test_events_property_returns_copy(self):
        from arcrun.events import EventBus

        bus = EventBus(run_id="r")
        bus.emit("a")
        events = bus.events
        events.clear()
        assert len(bus.events) == 1
