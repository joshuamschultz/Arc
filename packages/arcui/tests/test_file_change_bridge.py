"""Tests for FileChangeBridge — FileChangeEvent → per-client WS fan-out.

The bridge owns the mapping ``client queue → {subscribed agent_ids}`` plus a
bounded ring of recent :class:`FileChangeEvent`s for reconnect replay
(SDD §4.6 / PLAN 3.4).

Pillar 4 (scalability): subscriber registration is O(1) and broadcast is
O(clients) per event — no agent-scan on emit.
"""

from __future__ import annotations

import asyncio
import json

from arcgateway.file_events import FileChangeEvent, FileEventBus

from arcui.file_change_bridge import FileChangeBridge


def _evt(agent_id: str, event_type: str = "policy:bullets_updated") -> FileChangeEvent:
    return FileChangeEvent(
        agent_id=agent_id,
        event_type=event_type,
        path="workspace/policy.md",
        payload={"bullets": []},
    )


class TestSubscriptionRegistry:
    def test_add_and_remove_subscription(self) -> None:
        bridge = FileChangeBridge()
        q: asyncio.Queue[str] = asyncio.Queue()
        bridge.add_subscription(q, "a1")
        assert bridge.subscribers_for("a1") == {q}
        bridge.remove_subscription(q, "a1")
        assert bridge.subscribers_for("a1") == set()

    def test_remove_unknown_is_noop(self) -> None:
        bridge = FileChangeBridge()
        q: asyncio.Queue[str] = asyncio.Queue()
        bridge.remove_subscription(q, "ghost")  # must not raise

    def test_remove_all_for_drains_every_agent(self) -> None:
        bridge = FileChangeBridge()
        q: asyncio.Queue[str] = asyncio.Queue()
        bridge.add_subscription(q, "a1")
        bridge.add_subscription(q, "a2")
        agents = bridge.remove_all_for(q)
        assert agents == {"a1", "a2"}
        assert bridge.subscribers_for("a1") == set()
        assert bridge.subscribers_for("a2") == set()

    def test_two_clients_same_agent(self) -> None:
        bridge = FileChangeBridge()
        q1: asyncio.Queue[str] = asyncio.Queue()
        q2: asyncio.Queue[str] = asyncio.Queue()
        bridge.add_subscription(q1, "a1")
        bridge.add_subscription(q2, "a1")
        assert bridge.subscribers_for("a1") == {q1, q2}


class TestEmit:
    async def test_emit_sends_only_to_subscribed_clients(self) -> None:
        bridge = FileChangeBridge()
        q1: asyncio.Queue[str] = asyncio.Queue()
        q2: asyncio.Queue[str] = asyncio.Queue()
        bridge.add_subscription(q1, "a1")
        bridge.add_subscription(q2, "a2")

        await bridge.handle_event(_evt("a1"))

        assert q1.qsize() == 1
        assert q2.qsize() == 0

    async def test_emit_envelope_format(self) -> None:
        bridge = FileChangeBridge()
        q: asyncio.Queue[str] = asyncio.Queue()
        bridge.add_subscription(q, "a1")

        await bridge.handle_event(
            FileChangeEvent(
                agent_id="a1",
                event_type="config:updated",
                path="arcagent.toml",
                payload={"key": "value"},
            )
        )
        msg = json.loads(await q.get())
        assert msg["type"] == "file_change"
        assert msg["agent_id"] == "a1"
        assert msg["event_type"] == "config:updated"
        assert msg["path"] == "arcagent.toml"
        assert msg["payload"] == {"key": "value"}

    async def test_no_subscribers_is_noop(self) -> None:
        bridge = FileChangeBridge()
        await bridge.handle_event(_evt("a1"))  # must not raise


class TestBusIntegration:
    async def test_bridge_attaches_to_bus(self) -> None:
        bus = FileEventBus()
        bridge = FileChangeBridge()
        bridge.attach(bus)

        q: asyncio.Queue[str] = asyncio.Queue()
        bridge.add_subscription(q, "a1")

        await bus.emit(_evt("a1"))
        assert q.qsize() == 1

    async def test_detach_stops_receiving(self) -> None:
        bus = FileEventBus()
        bridge = FileChangeBridge()
        bridge.attach(bus)
        bridge.detach(bus)

        q: asyncio.Queue[str] = asyncio.Queue()
        bridge.add_subscription(q, "a1")

        await bus.emit(_evt("a1"))
        assert q.qsize() == 0


class TestReplay:
    """SDD §4.6 / PLAN 3.4: reconnect replay from a bounded ring."""

    async def test_replay_for_returns_recent_events_for_agent(self) -> None:
        bridge = FileChangeBridge(max_replay=50)
        await bridge.handle_event(_evt("a1", "policy:bullets_updated"))
        await bridge.handle_event(_evt("a2", "config:updated"))
        await bridge.handle_event(_evt("a1", "memory:updated"))

        q: asyncio.Queue[str] = asyncio.Queue()
        bridge.replay_for(q, "a1")

        msgs = [json.loads(await q.get()) for _ in range(q.qsize())]
        assert len(msgs) == 2
        assert all(m["agent_id"] == "a1" for m in msgs)
        assert msgs[0]["event_type"] == "policy:bullets_updated"
        assert msgs[1]["event_type"] == "memory:updated"

    async def test_replay_ring_is_bounded(self) -> None:
        bridge = FileChangeBridge(max_replay=3)
        for i in range(5):
            await bridge.handle_event(_evt("a1", f"evt:{i}"))

        q: asyncio.Queue[str] = asyncio.Queue()
        bridge.replay_for(q, "a1")

        msgs = [json.loads(await q.get()) for _ in range(q.qsize())]
        # Only the last 3 events kept
        assert len(msgs) == 3
        assert [m["event_type"] for m in msgs] == ["evt:2", "evt:3", "evt:4"]

    async def test_replay_for_unknown_agent_is_noop(self) -> None:
        bridge = FileChangeBridge()
        q: asyncio.Queue[str] = asyncio.Queue()
        bridge.replay_for(q, "ghost")
        assert q.empty()


class TestQueueFullIsResilient:
    async def test_full_queue_drops_oldest(self) -> None:
        bridge = FileChangeBridge()
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=2)
        bridge.add_subscription(q, "a1")

        await bridge.handle_event(_evt("a1", "evt:1"))
        await bridge.handle_event(_evt("a1", "evt:2"))
        await bridge.handle_event(_evt("a1", "evt:3"))  # would overflow

        assert q.qsize() == 2  # dropped oldest, no exception

    async def test_one_failing_client_does_not_block_others(self) -> None:
        bridge = FileChangeBridge()
        q_full: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        q_full.put_nowait("filler")  # already at capacity
        q_ok: asyncio.Queue[str] = asyncio.Queue()
        bridge.add_subscription(q_full, "a1")
        bridge.add_subscription(q_ok, "a1")

        await bridge.handle_event(_evt("a1"))

        # q_full had its old item dropped, q_ok received normally.
        assert q_ok.qsize() == 1


