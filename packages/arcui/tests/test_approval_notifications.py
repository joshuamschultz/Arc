from arcstore.approval_dispatcher import ApprovalNotification

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
