from arcstore.approval_dispatcher import ApprovalNotification
from arcstore.backends.memory import FakeBackend

from arcgateway.approval_notifications import (
    ApprovalNotificationFanout,
    GatewayApprovalNotificationSink,
    compose_approval_message,
)
from arcgateway.delivery import DeliveryTarget


class _Sink:
    def __init__(self) -> None:
        self.events = []

    async def __call__(self, event: ApprovalNotification) -> None:
        self.events.append(event)


async def test_fanout_delivers_sanitized_typed_event() -> None:
    event = ApprovalNotification(
        event_id="evt-1",
        approval_id="approval-1",
        status="pending",
        agent_did="did:agent:1",
        tool="send",
        classification="PERSONAL",
        attempts=1,
    )
    first, second = _Sink(), _Sink()
    fanout = ApprovalNotificationFanout([first])
    fanout.add(second)
    await fanout(event)
    assert first.events == second.events == [event]
    message = compose_approval_message(event)
    assert "evt-1" in message and "args" not in message and "grants" not in message


class _Router:
    def __init__(self) -> None:
        self.calls = []

    async def send(self, target, message, *, agent_did="") -> None:
        self.calls.append((target, message, agent_did))


async def test_configured_operator_target_uses_the_configured_sending_agent() -> None:
    event = ApprovalNotification(
        event_id="evt-1",
        approval_id="approval-1",
        status="pending",
        agent_did="did:agent:1",
        tool="send",
        classification="PERSONAL",
        attempts=1,
    )
    router = _Router()
    target = DeliveryTarget.parse("web:operator")
    await GatewayApprovalNotificationSink(router, target, agent_did="did:arc:gateway")(event)
    assert router.calls == [(target, compose_approval_message(event), "did:arc:gateway")]


async def test_gateway_sink_deduplicates_after_sink_restart() -> None:
    event = ApprovalNotification(
        event_id="evt-restart",
        approval_id="approval-1",
        status="pending",
        agent_did="did:agent:1",
        tool="send",
        classification="PERSONAL",
        attempts=1,
    )
    backend = FakeBackend()
    router = _Router()
    target = DeliveryTarget.parse("web:operator")
    await GatewayApprovalNotificationSink(router, target, backend=backend)(event)
    await GatewayApprovalNotificationSink(router, target, backend=backend)(event)
    assert len(router.calls) == 1


async def test_fanout_continues_after_partial_failure_without_duplicate_success() -> None:
    event = ApprovalNotification(
        event_id="evt-partial",
        approval_id="approval-1",
        status="pending",
        agent_did="did:agent:1",
        tool="send",
        classification="PERSONAL",
        attempts=1,
    )
    backend = FakeBackend()
    router = _Router()
    target = DeliveryTarget.parse("web:operator")
    gateway = GatewayApprovalNotificationSink(router, target, backend=backend)

    async def failing(_event: ApprovalNotification) -> None:
        raise RuntimeError("temporary")

    fanout = ApprovalNotificationFanout([gateway, failing])
    try:
        await fanout(event)
    except RuntimeError:
        pass
    else:
        raise AssertionError("fanout must report partial failure")
    try:
        await fanout(event)
    except RuntimeError:
        pass
    assert len(router.calls) == 1
