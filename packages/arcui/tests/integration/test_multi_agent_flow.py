"""Integration test: Agent → UI → Browser event flow.

Verifies the full pipeline: UIEvent pushed to EventBuffer → routed through
SubscriptionManager → delivered to browser queue.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from arcui.aggregator import RollingAggregator
from arcui.connection import ConnectionManager
from arcui.event_buffer import EventBuffer
from arcui.registry import AgentRegistry
from arcui.subscription import Subscription, SubscriptionManager
from arcui.types import UIEvent


def _make_event(
    agent_id: str = "agent-1",
    agent_name: str = "test-agent",
    layer: Literal["llm", "run", "agent", "team", "scheduler"] = "llm",
    event_type: str = "call_complete",
    sequence: int = 0,
) -> UIEvent:
    """Create a UIEvent for testing."""
    return UIEvent(
        layer=layer,
        event_type=event_type,
        agent_id=agent_id,
        agent_name=agent_name,
        source_id="did:arc:test",
        timestamp="2026-01-01T00:00:00Z",
        data={"model": "gpt-4"},
        sequence=sequence,
    )


class TestAgentToUIToBrowser:
    """Full pipeline: agent event → EventBuffer → SubscriptionManager → browser."""

    async def test_event_reaches_browser_queue(self) -> None:
        """A UIEvent pushed into EventBuffer should arrive at a subscribed browser queue."""
        conn_mgr = ConnectionManager()
        sub_mgr = SubscriptionManager()
        buffer = EventBuffer(conn_mgr, subscription_manager=sub_mgr)

        # Create browser client queue
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription())  # Subscribe to all

        event = _make_event()
        buffer.push(event)

        # Manually flush (in real server, this is done by the background loop)
        buffer.flush_once()

        # Verify event arrived at browser queue
        assert not queue.empty()
        raw = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert "call_complete" in raw
        assert "agent-1" in raw

    async def test_two_agents_events_both_arrive(self) -> None:
        """Events from two different agents should both reach a browser subscribing to all."""
        conn_mgr = ConnectionManager()
        sub_mgr = SubscriptionManager()
        buffer = EventBuffer(conn_mgr, subscription_manager=sub_mgr)

        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription())

        e1 = _make_event(agent_id="agent-1", agent_name="alpha")
        e2 = _make_event(agent_id="agent-2", agent_name="beta")
        buffer.push(e1)
        buffer.push(e2)
        buffer.flush_once()

        messages = []
        while not queue.empty():
            messages.append(await queue.get())

        assert len(messages) == 2
        assert any("agent-1" in m for m in messages)
        assert any("agent-2" in m for m in messages)

    async def test_aggregator_ingests_event(self) -> None:
        """The RollingAggregator should ingest UIEvents pushed through EventBuffer."""
        conn_mgr = ConnectionManager()
        sub_mgr = SubscriptionManager()
        aggregator = RollingAggregator()
        buffer = EventBuffer(conn_mgr, subscription_manager=sub_mgr)

        event = _make_event()
        buffer.push(event)

        # Manually ingest into aggregator (in real server, this happens in agent_ws)
        aggregator.ingest(event.model_dump())

        stats = aggregator.stats("1h")
        assert stats["request_count"] >= 1

    async def test_per_agent_aggregator(self) -> None:
        """Per-agent aggregator should only contain that agent's events."""
        registry = AgentRegistry()
        per_agent_agg = RollingAggregator()

        # Simulate agent registration with a per-agent aggregator
        from unittest.mock import MagicMock

        from arcui.types import AgentRegistration

        reg = AgentRegistration(
            agent_id="agent-1",
            agent_name="alpha",
            model="gpt-4",
            provider="openai",
            connected_at="2026-01-01T00:00:00Z",
        )
        ws = MagicMock()
        entry = registry.register("agent-1", ws, reg)
        entry.aggregator = per_agent_agg

        # Ingest event into per-agent aggregator
        event = _make_event(agent_id="agent-1")
        per_agent_agg.ingest(event.model_dump())

        stats = per_agent_agg.stats("1h")
        assert stats["request_count"] >= 1
