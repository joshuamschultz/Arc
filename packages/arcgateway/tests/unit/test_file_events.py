"""Tests for arcgateway.file_events — async pub/sub for FileChangeEvent."""

from __future__ import annotations

import asyncio

import pytest

from arcgateway.file_events import (
    FileChangeEvent,
    FileEventBus,
)


class TestFileChangeEvent:
    def test_construction_with_required_fields(self) -> None:
        evt = FileChangeEvent(
            agent_id="alice",
            event_type="policy:bullets_updated",
            path="workspace/policy.md",
        )
        assert evt.agent_id == "alice"
        assert evt.event_type == "policy:bullets_updated"
        assert evt.path == "workspace/policy.md"
        assert evt.payload == {}

    def test_construction_with_payload(self) -> None:
        evt = FileChangeEvent(
            agent_id="alice",
            event_type="policy:bullets_updated",
            path="workspace/policy.md",
            payload={"bullets": [{"id": "P01"}]},
        )
        assert evt.payload == {"bullets": [{"id": "P01"}]}


class TestFileEventBus:
    async def test_subscribe_emit_receive(self) -> None:
        bus = FileEventBus()
        received: list[FileChangeEvent] = []

        async def listener(evt: FileChangeEvent) -> None:
            received.append(evt)

        bus.subscribe(listener)
        evt = FileChangeEvent(agent_id="a", event_type="config:updated", path="arcagent.toml")
        await bus.emit(evt)
        # Give the scheduled task a chance to run.
        await asyncio.sleep(0)
        assert len(received) == 1
        assert received[0].agent_id == "a"

    async def test_multiple_subscribers_all_receive(self) -> None:
        bus = FileEventBus()
        a: list[FileChangeEvent] = []
        b: list[FileChangeEvent] = []

        async def la(e: FileChangeEvent) -> None:
            a.append(e)

        async def lb(e: FileChangeEvent) -> None:
            b.append(e)

        bus.subscribe(la)
        bus.subscribe(lb)
        await bus.emit(FileChangeEvent(agent_id="x", event_type="config:updated", path="p"))
        await asyncio.sleep(0)
        assert len(a) == 1
        assert len(b) == 1

    async def test_unsubscribe_stops_receiving(self) -> None:
        bus = FileEventBus()
        received: list[FileChangeEvent] = []

        async def listener(evt: FileChangeEvent) -> None:
            received.append(evt)

        bus.subscribe(listener)
        await bus.emit(FileChangeEvent(agent_id="x", event_type="t", path="p"))
        await asyncio.sleep(0)
        assert len(received) == 1

        bus.unsubscribe(listener)
        await bus.emit(FileChangeEvent(agent_id="x", event_type="t", path="p"))
        await asyncio.sleep(0)
        assert len(received) == 1  # unchanged

    async def test_unsubscribe_unknown_listener_is_noop(self) -> None:
        bus = FileEventBus()

        async def listener(evt: FileChangeEvent) -> None:
            pass

        # Not currently subscribed; should not raise.
        bus.unsubscribe(listener)

    async def test_listener_exception_does_not_break_bus(self) -> None:
        bus = FileEventBus()
        ok: list[FileChangeEvent] = []

        async def bad(_: FileChangeEvent) -> None:
            raise RuntimeError("boom")

        async def good(evt: FileChangeEvent) -> None:
            ok.append(evt)

        bus.subscribe(bad)
        bus.subscribe(good)
        await bus.emit(FileChangeEvent(agent_id="x", event_type="t", path="p"))
        await asyncio.sleep(0)
        # Good listener still got the event despite bad raising.
        assert len(ok) == 1

    async def test_no_subscribers_emit_is_noop(self) -> None:
        bus = FileEventBus()
        # Must not raise when there are no listeners.
        await bus.emit(FileChangeEvent(agent_id="x", event_type="t", path="p"))


class TestSyncSubscribe:
    """Subscribe must be safe to call before any event loop is running."""

    def test_subscribe_outside_event_loop(self) -> None:
        bus = FileEventBus()

        async def listener(evt: FileChangeEvent) -> None:
            pass

        # Should not raise even though no loop is running.
        bus.subscribe(listener)
        assert bus.subscriber_count() == 1


class TestSubscriberCount:
    def test_count_tracks_subscriptions(self) -> None:
        bus = FileEventBus()

        async def la(e: FileChangeEvent) -> None: ...
        async def lb(e: FileChangeEvent) -> None: ...

        assert bus.subscriber_count() == 0
        bus.subscribe(la)
        assert bus.subscriber_count() == 1
        bus.subscribe(lb)
        assert bus.subscriber_count() == 2
        bus.unsubscribe(la)
        assert bus.subscriber_count() == 1


@pytest.fixture(autouse=True)
def _reset_global_bus() -> None:
    """Reset the module-level singleton between tests."""
    from arcgateway import file_events

    file_events._reset_default_bus_for_tests()
