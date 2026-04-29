"""Integration test: Multi-agent subscription filtering.

3 agents connect with different capabilities. Browser subscribes to
specific agent + layer combinations. Verifies only matching events arrive.
"""

from __future__ import annotations

import json

from arcui.connection import ConnectionManager
from arcui.event_buffer import EventBuffer
from arcui.subscription import Subscription, SubscriptionManager
from arcui.types import UIEvent


def _make_event(
    agent_id: str,
    layer: str,
    event_type: str = "test",
    team: str | None = None,
    sequence: int = 0,
) -> UIEvent:
    return UIEvent(
        layer=layer,
        event_type=event_type,
        agent_id=agent_id,
        agent_name=f"agent-{agent_id}",
        source_id="did:arc:test",
        timestamp="2026-01-01T00:00:00Z",
        data={"team": team} if team else {},
        sequence=sequence,
    )


class TestMultiAgentSubscription:
    """Verify subscription filtering with multiple agents and browsers."""

    async def test_agent_filter_only_receives_matching(self) -> None:
        """Browser subscribing to agent-1 only receives agent-1 events."""
        conn_mgr = ConnectionManager()
        sub_mgr = SubscriptionManager()
        buffer = EventBuffer(conn_mgr, subscription_manager=sub_mgr)

        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription(agents=["agent-1"]))

        # Push events from 3 agents
        buffer.push(_make_event("agent-1", "llm", sequence=1))
        buffer.push(_make_event("agent-2", "llm", sequence=2))
        buffer.push(_make_event("agent-3", "llm", sequence=3))
        buffer.flush_once()

        messages = []
        while not queue.empty():
            messages.append(await queue.get())

        assert len(messages) == 1
        parsed = json.loads(messages[0])
        assert parsed["agent_id"] == "agent-1"

    async def test_layer_filter_only_receives_matching(self) -> None:
        """Browser subscribing to llm layer only receives llm events."""
        conn_mgr = ConnectionManager()
        sub_mgr = SubscriptionManager()
        buffer = EventBuffer(conn_mgr, subscription_manager=sub_mgr)

        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription(layers=["llm"]))

        buffer.push(_make_event("agent-1", "llm", sequence=1))
        buffer.push(_make_event("agent-1", "agent", sequence=2))
        buffer.push(_make_event("agent-1", "run", sequence=3))
        buffer.flush_once()

        messages = []
        while not queue.empty():
            messages.append(await queue.get())

        assert len(messages) == 1
        parsed = json.loads(messages[0])
        assert parsed["layer"] == "llm"

    async def test_combined_agent_and_layer_filter(self) -> None:
        """Browser filtering both agent and layer should get intersection."""
        conn_mgr = ConnectionManager()
        sub_mgr = SubscriptionManager()
        buffer = EventBuffer(conn_mgr, subscription_manager=sub_mgr)

        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription(agents=["agent-1"], layers=["llm"]))

        buffer.push(_make_event("agent-1", "llm", sequence=1))  # match
        buffer.push(_make_event("agent-1", "agent", sequence=2))  # wrong layer
        buffer.push(_make_event("agent-2", "llm", sequence=3))  # wrong agent
        buffer.push(_make_event("agent-2", "agent", sequence=4))  # wrong both
        buffer.flush_once()

        messages = []
        while not queue.empty():
            messages.append(await queue.get())

        assert len(messages) == 1
        parsed = json.loads(messages[0])
        assert parsed["agent_id"] == "agent-1"
        assert parsed["layer"] == "llm"

    async def test_subscribe_all_receives_everything(self) -> None:
        """Default Subscription (no filters) receives all events."""
        conn_mgr = ConnectionManager()
        sub_mgr = SubscriptionManager()
        buffer = EventBuffer(conn_mgr, subscription_manager=sub_mgr)

        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription())

        buffer.push(_make_event("agent-1", "llm", sequence=1))
        buffer.push(_make_event("agent-2", "agent", sequence=2))
        buffer.push(_make_event("agent-3", "run", sequence=3))
        buffer.flush_once()

        messages = []
        while not queue.empty():
            messages.append(await queue.get())

        assert len(messages) == 3

    async def test_two_browsers_different_subscriptions(self) -> None:
        """Two browsers with different subscriptions receive different events."""
        conn_mgr = ConnectionManager()
        sub_mgr = SubscriptionManager()
        buffer = EventBuffer(conn_mgr, subscription_manager=sub_mgr)

        q1 = conn_mgr.create_queue()
        q2 = conn_mgr.create_queue()
        sub_mgr.set_subscription(q1, Subscription(agents=["agent-1"]))
        sub_mgr.set_subscription(q2, Subscription(agents=["agent-2"]))

        buffer.push(_make_event("agent-1", "llm", sequence=1))
        buffer.push(_make_event("agent-2", "llm", sequence=2))
        buffer.flush_once()

        msgs1 = []
        while not q1.empty():
            msgs1.append(await q1.get())

        msgs2 = []
        while not q2.empty():
            msgs2.append(await q2.get())

        assert len(msgs1) == 1
        assert len(msgs2) == 1
        assert "agent-1" in msgs1[0]
        assert "agent-2" in msgs2[0]
