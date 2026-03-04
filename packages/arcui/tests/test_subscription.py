"""Tests for SubscriptionManager — server-side event filtering."""

from __future__ import annotations

import asyncio

from arcui.subscription import Subscription, SubscriptionManager
from arcui.types import UIEvent


def _make_event(
    agent_id: str = "a1",
    layer: str = "llm",
    team: str | None = None,
) -> UIEvent:
    return UIEvent(
        layer=layer,
        event_type="test",
        agent_id=agent_id,
        agent_name="test",
        source_id="src",
        timestamp="2026-03-03T12:00:00+00:00",
        data={"team": team} if team else {},
        sequence=0,
    )


class TestSubscription:
    def test_default_matches_all(self):
        sub = Subscription()
        assert sub.agents is None
        assert sub.layers is None
        assert sub.teams is None

    def test_specific_agents(self):
        sub = Subscription(agents=["a1", "a2"])
        assert "a1" in sub.agents
        assert "a3" not in sub.agents


class TestSubscriptionManagerMatches:
    def test_no_subscription_matches_everything(self):
        mgr = SubscriptionManager()
        queue: asyncio.Queue[str] = asyncio.Queue()
        # No subscription set — default is match all
        event = _make_event()
        assert mgr.matches(queue, event) is True

    def test_subscribe_to_specific_agent(self):
        mgr = SubscriptionManager()
        queue: asyncio.Queue[str] = asyncio.Queue()
        mgr.set_subscription(queue, Subscription(agents=["a1"]))

        assert mgr.matches(queue, _make_event(agent_id="a1")) is True
        assert mgr.matches(queue, _make_event(agent_id="a2")) is False

    def test_subscribe_to_specific_layer(self):
        mgr = SubscriptionManager()
        queue: asyncio.Queue[str] = asyncio.Queue()
        mgr.set_subscription(queue, Subscription(layers=["llm", "run"]))

        assert mgr.matches(queue, _make_event(layer="llm")) is True
        assert mgr.matches(queue, _make_event(layer="run")) is True
        assert mgr.matches(queue, _make_event(layer="agent")) is False

    def test_subscribe_combined_filter(self):
        mgr = SubscriptionManager()
        queue: asyncio.Queue[str] = asyncio.Queue()
        mgr.set_subscription(queue, Subscription(agents=["a1"], layers=["llm"]))

        assert mgr.matches(queue, _make_event(agent_id="a1", layer="llm")) is True
        assert mgr.matches(queue, _make_event(agent_id="a1", layer="run")) is False
        assert mgr.matches(queue, _make_event(agent_id="a2", layer="llm")) is False

    def test_unregistered_queue_matches_all(self):
        """Queue not in manager should default to matching everything."""
        mgr = SubscriptionManager()
        unknown_queue: asyncio.Queue[str] = asyncio.Queue()
        assert mgr.matches(unknown_queue, _make_event()) is True


class TestSubscriptionManagerBroadcast:
    def test_broadcast_filtered_sends_to_matching(self):
        mgr = SubscriptionManager()
        q1: asyncio.Queue[str] = asyncio.Queue()
        q2: asyncio.Queue[str] = asyncio.Queue()

        mgr.set_subscription(q1, Subscription(agents=["a1"]))
        mgr.set_subscription(q2, Subscription(agents=["a2"]))

        event = _make_event(agent_id="a1")
        mgr.broadcast_filtered(event)

        assert not q1.empty()
        assert q2.empty()

    def test_broadcast_filtered_sends_to_all_with_no_filter(self):
        mgr = SubscriptionManager()
        q1: asyncio.Queue[str] = asyncio.Queue()
        q2: asyncio.Queue[str] = asyncio.Queue()

        mgr.set_subscription(q1, Subscription())  # All
        mgr.set_subscription(q2, Subscription())  # All

        mgr.broadcast_filtered(_make_event())

        assert not q1.empty()
        assert not q2.empty()

    def test_remove_subscription(self):
        mgr = SubscriptionManager()
        queue: asyncio.Queue[str] = asyncio.Queue()
        mgr.set_subscription(queue, Subscription(agents=["a1"]))
        mgr.remove_subscription(queue)

        # After removal, broadcast should not target this queue
        mgr.broadcast_filtered(_make_event(agent_id="a1"))
        assert queue.empty()

    def test_team_filter_matches(self):
        mgr = SubscriptionManager()
        queue: asyncio.Queue[str] = asyncio.Queue()
        mgr.set_subscription(queue, Subscription(teams=["alpha"]))

        assert mgr.matches(queue, _make_event(team="alpha")) is True
        assert mgr.matches(queue, _make_event(team="beta")) is False
        assert mgr.matches(queue, _make_event()) is False

    def test_broadcast_drops_oldest_on_full_queue(self):
        mgr = SubscriptionManager()
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=2)
        mgr.set_subscription(queue, Subscription())

        # Fill the queue
        mgr.broadcast_filtered(_make_event(agent_id="a1"))
        mgr.broadcast_filtered(_make_event(agent_id="a2"))
        assert queue.qsize() == 2

        # Third message should drop oldest
        mgr.broadcast_filtered(_make_event(agent_id="a3"))
        assert queue.qsize() == 2
