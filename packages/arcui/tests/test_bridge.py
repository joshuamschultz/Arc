"""Tests for UIBridgeSink — arctrust.AuditEvent → arcui.UIEvent round-trip.

Verifies that:
- UIBridgeSink satisfies the AuditSink protocol (structural subtyping).
- An AuditEvent emitted through UIBridgeSink produces a corresponding UIEvent
  on every subscribed browser queue with correct field mapping.
- ASI07 / AU-2: bridge is a single emission point; no double-emit.
- AU-9: bridge does not lose any AuditEvent fields (stored in UIEvent.data).
"""

from __future__ import annotations

import asyncio

from arctrust import AuditEvent, AuditSink, emit

from arcui.bridge import UIBridgeSink
from arcui.connection import ConnectionManager
from arcui.event_buffer import EventBuffer
from arcui.subscription import Subscription, SubscriptionManager


def _make_pipeline() -> tuple[EventBuffer, ConnectionManager, SubscriptionManager]:
    """Create a minimal in-memory pipeline for bridge testing."""
    conn_mgr = ConnectionManager()
    sub_mgr = SubscriptionManager()
    buffer = EventBuffer(conn_mgr, subscription_manager=sub_mgr)
    return buffer, conn_mgr, sub_mgr


class TestUIBridgeSinkProtocol:
    """UIBridgeSink must satisfy the AuditSink structural protocol."""

    def test_satisfies_audit_sink_protocol(self) -> None:
        buffer, _, _ = _make_pipeline()
        sink = UIBridgeSink(event_buffer=buffer)
        # runtime_checkable Protocol check
        assert isinstance(sink, AuditSink)

    def test_has_write_method(self) -> None:
        buffer, _, _ = _make_pipeline()
        sink = UIBridgeSink(event_buffer=buffer)
        assert callable(getattr(sink, "write", None))


class TestUIBridgeSinkMapping:
    """Field mapping: AuditEvent fields → UIEvent fields."""

    async def test_audit_event_produces_ui_event(self) -> None:
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription())  # subscribe to all

        sink = UIBridgeSink(event_buffer=buffer)
        event = AuditEvent(
            actor_did="did:arc:local:executor/abc123",
            action="tool.call",
            target="read_file",
            outcome="allow",
            tier="enterprise",
            request_id="req-abc-001",
        )
        sink.write(event)
        buffer.flush_once()

        assert not queue.empty(), "UIEvent should have been pushed to the browser queue"
        raw = await asyncio.wait_for(queue.get(), timeout=1.0)

        # UIEvent JSON should contain the actor_did mapped to agent_id
        assert "did:arc:local:executor/abc123" in raw

    async def test_actor_did_maps_to_agent_id(self) -> None:
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription())

        sink = UIBridgeSink(event_buffer=buffer)
        event = AuditEvent(
            actor_did="did:arc:local:executor/myagent",
            action="memory.write",
            target="context.md",
            outcome="allow",
        )
        sink.write(event)
        buffer.flush_once()

        raw = await queue.get()
        import json

        data = json.loads(raw)
        assert data["agent_id"] == "did:arc:local:executor/myagent"

    async def test_action_maps_to_event_type_with_underscore(self) -> None:
        """Dots in action names are replaced with underscores for UIEvent.event_type."""
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription())

        sink = UIBridgeSink(event_buffer=buffer)
        event = AuditEvent(
            actor_did="did:arc:local:executor/agent1",
            action="policy.evaluate",
            target="tool_use",
            outcome="deny",
        )
        sink.write(event)
        buffer.flush_once()

        raw = await queue.get()
        import json

        data = json.loads(raw)
        # Dots replaced with underscores to satisfy UIEvent pattern constraint
        assert data["event_type"] == "policy_evaluate"

    async def test_tier_stored_in_meta_within_data(self) -> None:
        """tier from AuditEvent should appear in UIEvent.data.meta."""
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription())

        sink = UIBridgeSink(event_buffer=buffer)
        event = AuditEvent(
            actor_did="did:arc:local:executor/a1",
            action="auth_check",
            target="tool",
            outcome="allow",
            tier="federal",
        )
        sink.write(event)
        buffer.flush_once()

        raw = await queue.get()
        import json

        data = json.loads(raw)
        assert data["data"]["tier"] == "federal"

    async def test_outcome_stored_in_data(self) -> None:
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription())

        sink = UIBridgeSink(event_buffer=buffer)
        event = AuditEvent(
            actor_did="did:arc:local:executor/a1",
            action="policy_eval",
            target="tool",
            outcome="deny",
        )
        sink.write(event)
        buffer.flush_once()

        raw = await queue.get()
        import json

        data = json.loads(raw)
        assert data["data"]["outcome"] == "deny"

    async def test_target_stored_in_data(self) -> None:
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription())

        sink = UIBridgeSink(event_buffer=buffer)
        event = AuditEvent(
            actor_did="did:arc:local:executor/a1",
            action="tool_call",
            target="write_file",
            outcome="allow",
        )
        sink.write(event)
        buffer.flush_once()

        raw = await queue.get()
        import json

        data = json.loads(raw)
        assert data["data"]["target"] == "write_file"

    async def test_request_id_stored_in_data(self) -> None:
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription())

        sink = UIBridgeSink(event_buffer=buffer)
        event = AuditEvent(
            actor_did="did:arc:local:executor/a1",
            action="tool_call",
            target="search",
            outcome="allow",
            request_id="trace-xyz-999",
        )
        sink.write(event)
        buffer.flush_once()

        raw = await queue.get()
        import json

        data = json.loads(raw)
        assert data["data"]["request_id"] == "trace-xyz-999"

    async def test_layer_is_agent(self) -> None:
        """Audit events from arctrust map to the 'agent' layer by default."""
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription())

        sink = UIBridgeSink(event_buffer=buffer)
        event = AuditEvent(
            actor_did="did:arc:local:executor/a1",
            action="tool_call",
            target="tool",
            outcome="allow",
        )
        sink.write(event)
        buffer.flush_once()

        raw = await queue.get()
        import json

        data = json.loads(raw)
        assert data["layer"] == "agent"

    def test_custom_layer_respected(self) -> None:
        """UIBridgeSink can be configured with a non-default layer."""
        buffer, _, _ = _make_pipeline()
        sink = UIBridgeSink(event_buffer=buffer, layer="team")
        # Verify the layer is stored
        assert sink.layer == "team"


class TestUIBridgeSinkViaEmit:
    """Test that arctrust.emit() calls UIBridgeSink.write() and the event flows."""

    async def test_emit_via_arctrust_reaches_browser(self) -> None:
        """Single emission point: arctrust.emit(event, UIBridgeSink) → UIEvent → browser."""
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription())

        sink = UIBridgeSink(event_buffer=buffer)
        event = AuditEvent(
            actor_did="did:arc:local:executor/emit-test",
            action="auth_success",
            target="agent_ws",
            outcome="allow",
        )

        # Use canonical arctrust.emit() — this is the single emission point
        emit(event, sink)
        buffer.flush_once()

        assert not queue.empty()
        raw = await queue.get()
        assert "emit-test" in raw

    async def test_no_double_emit(self) -> None:
        """A single write() call produces exactly one UIEvent on the browser queue."""
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription())

        sink = UIBridgeSink(event_buffer=buffer)
        event = AuditEvent(
            actor_did="did:arc:local:executor/a1",
            action="tool_call",
            target="tool",
            outcome="allow",
        )
        sink.write(event)
        buffer.flush_once()

        # Drain the queue
        count = 0
        while not queue.empty():
            await queue.get()
            count += 1

        assert count == 1, f"Expected exactly 1 UIEvent, got {count}"


class TestUIBridgeSinkLayerFilter:
    """Server-side subscription filtering works with UIBridgeSink events."""

    async def test_layer_filter_blocks_unmatched_subscriber(self) -> None:
        """A browser subscribed only to 'llm' layer should NOT receive 'agent' events."""
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        llm_queue = conn_mgr.create_queue()
        agent_queue = conn_mgr.create_queue()

        sub_mgr.set_subscription(llm_queue, Subscription(layers=["llm"]))
        sub_mgr.set_subscription(agent_queue, Subscription(layers=["agent"]))

        sink = UIBridgeSink(event_buffer=buffer, layer="agent")
        event = AuditEvent(
            actor_did="did:arc:local:executor/a1",
            action="tool_call",
            target="tool",
            outcome="allow",
        )
        sink.write(event)
        buffer.flush_once()

        # agent_queue should have 1 event; llm_queue should be empty
        assert not agent_queue.empty()
        assert llm_queue.empty()

    async def test_all_subscriber_receives_agent_event(self) -> None:
        """A browser with no filter receives bridge events on any layer."""
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription())  # no filter

        sink = UIBridgeSink(event_buffer=buffer, layer="agent")
        event = AuditEvent(
            actor_did="did:arc:local:executor/a1",
            action="tool_call",
            target="tool",
            outcome="allow",
        )
        sink.write(event)
        buffer.flush_once()

        assert not queue.empty()


class TestUIBridgeSinkSequencing:
    """Sequence numbers increment correctly across multiple events."""

    async def test_sequence_increments(self) -> None:
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription())

        sink = UIBridgeSink(event_buffer=buffer)
        for i in range(3):
            sink.write(
                AuditEvent(
                    actor_did="did:arc:local:executor/a1",
                    action=f"event_{i}",
                    target="t",
                    outcome="allow",
                )
            )
        buffer.flush_once()

        import json

        seqs = []
        while not queue.empty():
            raw = await queue.get()
            seqs.append(json.loads(raw)["sequence"])

        assert seqs == sorted(seqs), "Sequences should be monotonically increasing"
        assert len(set(seqs)) == 3, "Each event should have a unique sequence number"
