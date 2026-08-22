from arcstore.approval_dispatcher import ApprovalNotification
from arcstore.backends.memory import FakeBackend

from arcui.approval_notifications import ApprovalNotificationHub


async def test_hub_deduplicates_and_bounds_events() -> None:
    hub = ApprovalNotificationHub(max_events=1)

    def make(event_id: str) -> ApprovalNotification:
        return ApprovalNotification(
            event_id=event_id,
            approval_id=event_id,
            status="pending",
            agent_did=None,
            tool="tool",
            classification="UNCLASSIFIED",
            attempts=1,
        )

    await hub(make("one"))
    await hub(make("one"))
    await hub(make("two"))
    assert [event["event_id"] for event in hub.recent()] == ["two"]


async def test_hub_replays_unacknowledged_events_after_restart() -> None:
    backend = FakeBackend()

    def make(event_id: str) -> ApprovalNotification:
        return ApprovalNotification(
            event_id=event_id,
            approval_id=event_id,
            status="pending",
            agent_did=None,
            tool="tool",
            classification="UNCLASSIFIED",
            attempts=1,
        )

    await ApprovalNotificationHub(backend=backend)(make("restart"))
    restarted = ApprovalNotificationHub(backend=backend)
    assert [event["event_id"] for event in await restarted.list_recent()] == ["restart"]
    assert await restarted.acknowledge("restart")
    assert await restarted.list_recent() == []


async def test_hub_durable_dedupes_event_delivery() -> None:
    backend = FakeBackend()
    event = ApprovalNotification(
        event_id="once",
        approval_id="once",
        status="pending",
        agent_did=None,
        tool="tool",
        classification="UNCLASSIFIED",
        attempts=1,
    )
    await ApprovalNotificationHub(backend=backend)(event)
    await ApprovalNotificationHub(backend=backend)(event)
    rows = await backend.mutable_query("approval_notification_browser")
    assert len(rows) == 1
