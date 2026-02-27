"""Adversarial: Event chain tampering (NIST AU-9).

Tests that the SHA-256 hash chain detects all forms of event manipulation.
"""

from __future__ import annotations

import pytest

from arcrun.events import (
    GENESIS_PREV_HASH,
    Event,
    EventBus,
    verify_chain,
)


class TestEventTampering:
    def test_modify_event_data_after_emit_blocked(self):
        """Frozen dataclass + MappingProxyType prevents post-emit mutation."""
        bus = EventBus(run_id="test")
        event = bus.emit("secret", {"level": "classified"})

        # Cannot mutate field
        with pytest.raises(AttributeError):
            event.type = "public"  # type: ignore[misc]

        # Cannot mutate data dict
        with pytest.raises(TypeError):
            event.data["level"] = "unclassified"  # type: ignore[index]

    def test_modify_event_data_copy_detected_by_chain(self):
        """Creating a modified copy of an event is detected by verify_chain."""
        bus = EventBus(run_id="test")
        bus.emit("original", {"secret": "classified-data"})
        bus.emit("next")

        events = bus.events
        # Create a tampered copy with different data but same hash
        tampered = Event(
            type=events[0].type,
            timestamp=events[0].timestamp,
            run_id=events[0].run_id,
            data={"secret": "REDACTED"},
            sequence=events[0].sequence,
            prev_hash=events[0].prev_hash,
            event_hash=events[0].event_hash,
        )
        events[0] = tampered

        result = verify_chain(events)
        assert result.valid is False
        assert result.first_broken_index == 0
        assert result.error == "self-hash mismatch"

    def test_insert_fabricated_event(self):
        """Inserting an event breaks the chain."""
        bus = EventBus(run_id="test")
        bus.emit("a")
        bus.emit("c")

        events = bus.events
        fabricated = Event(
            type="injected",
            timestamp=0.0,
            run_id="test",
            data={"malicious": True},
            sequence=1,
            prev_hash=events[0].event_hash,
            event_hash="deadbeef" * 8,
        )
        events.insert(1, fabricated)

        result = verify_chain(events)
        assert result.valid is False

    def test_delete_event_from_chain(self):
        """Removing an event breaks the chain."""
        bus = EventBus(run_id="test")
        bus.emit("a")
        bus.emit("b")
        bus.emit("c")

        events = bus.events
        del events[1]

        result = verify_chain(events)
        assert result.valid is False

    def test_reorder_events(self):
        """Swapping events breaks the chain."""
        bus = EventBus(run_id="test")
        bus.emit("first")
        bus.emit("second")
        bus.emit("third")

        events = bus.events
        events[1], events[2] = events[2], events[1]

        result = verify_chain(events)
        assert result.valid is False

    def test_replay_events_from_different_run(self):
        """Events from one run cannot be replayed into another."""
        bus1 = EventBus(run_id="run-1")
        bus1.emit("authentic", {"data": "legit"})

        bus2 = EventBus(run_id="run-2")
        bus2.emit("authentic", {"data": "legit"})

        # Mix events from different runs
        mixed = [bus1.events[0], bus2.events[0]]

        result = verify_chain(mixed)
        # Chain break because bus2's event has its own prev_hash (genesis),
        # but position 1 should have bus1's event hash as prev_hash
        assert result.valid is False

    def test_genesis_hash_is_correct(self):
        """First event must reference GENESIS_PREV_HASH."""
        bus = EventBus(run_id="test")
        event = bus.emit("genesis")

        assert event.prev_hash == GENESIS_PREV_HASH
        assert event.prev_hash == "0" * 64

    def test_empty_chain_is_valid(self):
        """No events = valid chain."""
        result = verify_chain([])
        assert result.valid is True
        assert result.event_count == 0

    def test_chain_verification_result_has_details(self):
        """ChainVerificationResult provides actionable details on failure."""
        bus = EventBus(run_id="test")
        bus.emit("a")
        bus.emit("b")
        bus.emit("c")

        events = bus.events
        events.pop(1)  # Remove middle

        result = verify_chain(events)
        assert not result.valid
        assert result.event_count == 2
        assert result.first_broken_index is not None
        assert result.error is not None
