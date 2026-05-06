"""Unit tests for arcgateway.dashboard_events.DashboardEventBus.

SPEC-025 Track E (PLAN.md E1).

Covers:
  - subscribe() replays last-known value into the queue immediately.
  - publish() fans out to all current subscribers.
  - publish() stores the value; a subsequent subscribe() replays it.
  - drop-oldest backpressure when a subscriber queue is full.
  - unsubscribe() removes the queue from all topics.
  - no replay when topic has never been published.
  - last_value() helper.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arcgateway.dashboard_events import DashboardEventBus

pytestmark = pytest.mark.asyncio


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_queue(maxsize: int = 100) -> asyncio.Queue[dict[str, Any]]:
    return asyncio.Queue(maxsize=maxsize)


def _drain_queue(q: asyncio.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drain all currently queued items without blocking."""
    items: list[dict[str, Any]] = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestSubscribe:
    async def test_subscribe_no_replay_when_topic_never_published(self) -> None:
        """Subscribing to an unpublished topic enqueues nothing."""
        bus = DashboardEventBus()
        q = _make_queue()
        bus.subscribe(q, ["stats"])
        assert q.empty()

    async def test_subscribe_replays_last_value_immediately(self) -> None:
        """Subscribe replays the last-published value into the queue on subscribe."""
        bus = DashboardEventBus()
        payload = {"request_count": 42}
        await bus.publish("stats", payload)

        q = _make_queue()
        bus.subscribe(q, ["stats"])

        assert q.qsize() == 1
        frame = q.get_nowait()
        assert frame["topic"] == "stats"
        assert frame["payload"] == payload

    async def test_subscribe_replays_multiple_topics(self) -> None:
        """All subscribed topics with prior values are replayed."""
        bus = DashboardEventBus()
        await bus.publish("stats", {"x": 1})
        await bus.publish("queue", {"y": 2})

        q = _make_queue()
        bus.subscribe(q, ["stats", "queue", "budget"])  # budget never published

        frames = _drain_queue(q)
        topics = {f["topic"] for f in frames}
        assert topics == {"stats", "queue"}  # budget not replayed
        assert q.empty()

    async def test_subscribe_replay_skipped_for_unpublished_topics(self) -> None:
        """Topics without a prior value produce no replay frame."""
        bus = DashboardEventBus()
        q = _make_queue()
        bus.subscribe(q, ["stats", "queue"])
        assert q.empty()


class TestPublish:
    async def test_publish_distributes_to_all_subscribers(self) -> None:
        """publish() fans out to every registered queue."""
        bus = DashboardEventBus()
        q1, q2 = _make_queue(), _make_queue()
        bus.subscribe(q1, ["stats"])
        bus.subscribe(q2, ["stats"])

        payload = {"request_count": 10}
        await bus.publish("stats", payload)

        assert q1.qsize() == 1
        assert q2.qsize() == 1
        assert q1.get_nowait()["payload"] == payload
        assert q2.get_nowait()["payload"] == payload

    async def test_publish_does_not_leak_to_other_topics(self) -> None:
        """Subscribers on topic A do not receive publishes on topic B."""
        bus = DashboardEventBus()
        q = _make_queue()
        bus.subscribe(q, ["stats"])
        await bus.publish("queue", {"depth": 5})
        assert q.empty()

    async def test_publish_stores_last_value(self) -> None:
        """last_value() returns the most recent payload after publish."""
        bus = DashboardEventBus()
        await bus.publish("budget", {"budgets": []})
        await bus.publish("budget", {"budgets": [{"monthly_spend": 1.5}]})
        assert bus.last_value("budget") == {"budgets": [{"monthly_spend": 1.5}]}

    async def test_publish_overwrites_previous_last_value(self) -> None:
        """Subsequent publishes overwrite the stored value for replay."""
        bus = DashboardEventBus()
        await bus.publish("stats", {"request_count": 1})
        await bus.publish("stats", {"request_count": 2})

        q = _make_queue()
        bus.subscribe(q, ["stats"])
        frame = q.get_nowait()
        # Only the latest value is replayed.
        assert frame["payload"]["request_count"] == 2

    async def test_publish_to_topic_with_no_subscribers_stores_value(self) -> None:
        """Publish without subscribers still caches the value for replay."""
        bus = DashboardEventBus()
        await bus.publish("roster", {"agents": []})
        assert bus.last_value("roster") == {"agents": []}

    async def test_publish_multiple_topics_independently(self) -> None:
        """Separate topics are stored and distributed independently."""
        bus = DashboardEventBus()
        q = _make_queue()
        bus.subscribe(q, ["stats", "queue"])

        await bus.publish("stats", {"request_count": 7})
        await bus.publish("queue", {"queues": [{"depth": 3}]})

        frames = _drain_queue(q)
        by_topic = {f["topic"]: f["payload"] for f in frames}
        assert by_topic["stats"]["request_count"] == 7
        assert by_topic["queue"]["queues"][0]["depth"] == 3


class TestBackpressure:
    async def test_drop_oldest_when_queue_full(self) -> None:
        """When the subscriber queue is full, publish() drops the oldest frame."""
        bus = DashboardEventBus()
        q = _make_queue(maxsize=2)
        bus.subscribe(q, ["stats"])

        # Fill the queue with two items first (bypassing publish for speed).
        q.put_nowait({"topic": "stats", "payload": {"x": 0}})
        q.put_nowait({"topic": "stats", "payload": {"x": 1}})
        assert q.full()

        # Publish a third — should drop oldest and enqueue new.
        await bus.publish("stats", {"x": 2})

        frames = _drain_queue(q)
        assert len(frames) == 2
        # The oldest frame (x=0) was dropped; x=1 and x=2 remain.
        payloads = [f["payload"]["x"] for f in frames]
        assert 2 in payloads

    async def test_full_queue_still_accepts_publish(self) -> None:
        """publish() does not raise even when the subscriber queue is full."""
        bus = DashboardEventBus()
        q = _make_queue(maxsize=1)
        bus.subscribe(q, ["performance"])
        q.put_nowait({"topic": "performance", "payload": {}})
        # Must not raise.
        await bus.publish("performance", {"latency_avg": 42.0})
        # Queue has exactly 1 item (oldest was dropped, new one was added).
        assert q.qsize() == 1


class TestUnsubscribe:
    async def test_unsubscribe_stops_receiving_events(self) -> None:
        """After unsubscribe(), the queue receives no further publishes."""
        bus = DashboardEventBus()
        q = _make_queue()
        bus.subscribe(q, ["stats"])
        bus.unsubscribe(q)

        await bus.publish("stats", {"request_count": 99})
        assert q.empty()

    async def test_unsubscribe_idempotent(self) -> None:
        """Calling unsubscribe() twice does not raise."""
        bus = DashboardEventBus()
        q = _make_queue()
        bus.subscribe(q, ["stats"])
        bus.unsubscribe(q)
        bus.unsubscribe(q)  # idempotent

    async def test_unsubscribe_removes_from_all_topics(self) -> None:
        """unsubscribe() removes the queue from every topic it was registered on."""
        bus = DashboardEventBus()
        q = _make_queue()
        bus.subscribe(q, ["stats", "queue", "budget"])
        bus.unsubscribe(q)

        await bus.publish("stats", {"x": 1})
        await bus.publish("queue", {"y": 2})
        await bus.publish("budget", {"z": 3})
        assert q.empty()


class TestLastValue:
    async def test_last_value_none_before_publish(self) -> None:
        bus = DashboardEventBus()
        assert bus.last_value("stats") is None

    async def test_last_value_after_publish(self) -> None:
        bus = DashboardEventBus()
        await bus.publish("circuit_breakers", {"circuit_breakers": []})
        assert bus.last_value("circuit_breakers") == {"circuit_breakers": []}


# SPEC-025 §M-1 — TTL on last_value
# SPEC-025 §M-2 — audit on drop


class TestLastValueTTL:
    """Subscribe replay must respect the configured last_value TTL."""

    async def test_replay_skipped_when_value_older_than_ttl(self) -> None:
        bus = DashboardEventBus(last_value_ttl_seconds=0.01)  # 10 ms
        await bus.publish("stats", {"x": 1})
        # Sleep past the TTL window
        await asyncio.sleep(0.05)
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10)
        bus.subscribe(q, ["stats"])
        # No replay frame should be queued — TTL gated it.
        assert q.empty()

    async def test_replay_delivered_when_value_within_ttl(self) -> None:
        bus = DashboardEventBus(last_value_ttl_seconds=10.0)
        await bus.publish("stats", {"x": 1})
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10)
        bus.subscribe(q, ["stats"])
        frame = await q.get()
        assert frame == {"topic": "stats", "payload": {"x": 1}}


class TestDropOldestAudit:
    """SPEC-025 §M-2 — drop-oldest must emit an audit event for every drop."""

    async def test_drop_oldest_emits_audit_event(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []

        def _capture(action: str, data: dict[str, Any]) -> None:
            events.append((action, data))

        bus = DashboardEventBus(audit_emitter=_capture)
        # Tiny queue so we hit the drop branch immediately.
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
        bus.subscribe(q, ["queue"])
        await bus.publish("queue", {"depth": 1})  # fits
        await bus.publish("queue", {"depth": 2})  # forces drop-oldest

        actions = [a for a, _ in events]
        assert "gateway.dashboard.dropped_backpressure" in actions
        # Find the drop event and verify it carries the topic
        drop = next(d for a, d in events if a == "gateway.dashboard.dropped_backpressure")
        assert drop["topic"] == "queue"
        assert drop["reason"] == "queue_full"
