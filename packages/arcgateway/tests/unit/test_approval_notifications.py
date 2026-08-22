from arcstore.approval_dispatcher import ApprovalNotification

from arcgateway.approval_notifications import ApprovalNotificationFanout, compose_approval_message


class _Sink:
    def __init__(self) -> None:
        self.events = []

    async def __call__(self, event: ApprovalNotification) -> None:
        self.events.append(event)


async def test_fanout_delivers_sanitized_typed_event() -> None:
    event = ApprovalNotification(
        event_id="evt-1", approval_id="approval-1", status="pending",
        agent_did="did:agent:1", tool="send", classification="PERSONAL", attempts=1,
    )
    first, second = _Sink(), _Sink()
    fanout = ApprovalNotificationFanout([first])
    fanout.add(second)
    await fanout(event)
    assert first.events == second.events == [event]
    message = compose_approval_message(event)
    assert "evt-1" in message and "args" not in message and "grants" not in message
