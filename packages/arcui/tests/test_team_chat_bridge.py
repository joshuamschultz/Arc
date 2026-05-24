"""Tests for arcui.team_chat_bridge — arcteam route → dashboard_bus."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcui.team_chat_bridge import TeamChatDashboardBridge


class _FakeBus:
    """Minimal duck-typed dashboard bus."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self.published.append((topic, dict(payload)))


@pytest.mark.asyncio
async def test_message_route_event_publishes_to_team_chat_topic() -> None:
    bus = _FakeBus()
    bridge = TeamChatDashboardBridge(bus)
    bridge.emit_team_event(
        event_type="message_route",
        data={
            "to": ["agent://architect"],
            "message_id": "msg_1",
            "channel": "arc.channel.access-review-001",
            "sender": "agent://intake",
            "seq": 1,
        },
    )
    # The publish is scheduled on the loop — yield once so it runs.
    for _ in range(3):
        await asyncio.sleep(0)
    assert len(bus.published) == 1
    topic, payload = bus.published[0]
    assert topic == "team_chat"
    assert payload["sender"] == "agent://intake"
    assert payload["seq"] == 1


@pytest.mark.asyncio
async def test_non_route_events_are_ignored() -> None:
    """Only message_route events translate to team_chat frames."""
    bus = _FakeBus()
    bridge = TeamChatDashboardBridge(bus)
    bridge.emit_team_event(event_type="message_sent", data={"x": 1})
    for _ in range(3):
        await asyncio.sleep(0)
    assert bus.published == []


def test_no_running_loop_drops_silently() -> None:
    """The bridge is called from MessagingService.send — it must not
    raise when no asyncio loop is running."""
    bus = _FakeBus()
    bridge = TeamChatDashboardBridge(bus)
    # Should not raise.
    bridge.emit_team_event(event_type="message_route", data={"x": 1})
    # Synchronously: nothing should have published.
    assert bus.published == []


@pytest.mark.asyncio
async def test_none_bus_is_safe() -> None:
    """Tests can construct a bridge with no bus; emit must be a no-op."""
    bridge = TeamChatDashboardBridge(None)
    bridge.emit_team_event(event_type="message_route", data={"x": 1})
    # No exception, no scheduling.


@pytest.mark.asyncio
async def test_tracks_pending_tasks_until_publish_completes() -> None:
    """Strong references on pending tasks prevent GC before publish runs."""
    bus_publish_done = asyncio.Event()
    bus = MagicMock()

    async def slow_publish(topic: str, payload: Any) -> None:
        await asyncio.sleep(0.01)
        bus_publish_done.set()

    bus.publish = AsyncMock(side_effect=slow_publish)
    bridge = TeamChatDashboardBridge(bus)
    bridge.emit_team_event(event_type="message_route", data={"x": 1})
    assert len(bridge._pending) == 1
    await bus_publish_done.wait()
    await asyncio.sleep(0)
    assert len(bridge._pending) == 0
