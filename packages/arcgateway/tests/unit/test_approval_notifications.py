import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from arcstore.approval_delivery import DurableApprovalDelivery
from arcstore.approval_dispatcher import (
    ApprovalDeliveryResult,
    ApprovalDispatcherConfig,
    ApprovalNotification,
    ApprovalNotificationDispatcher,
)
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


@pytest.mark.asyncio
async def test_crash_before_complete_is_reclaimed_after_delivery_lease_expires() -> None:
    event = ApprovalNotification(
        event_id="evt-crash",
        approval_id="approval-1",
        status="pending",
        attempts=1,
    )
    backend = FakeBackend()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = DurableApprovalDelivery(
        backend,
        sink_id="gateway-test",
        clock=lambda: now,
        lease_seconds=30,
    )
    assert (await first.claim(event)).owner_token is not None

    recovered = DurableApprovalDelivery(
        backend,
        sink_id="gateway-test",
        clock=lambda: now + timedelta(seconds=31),
        lease_seconds=30,
    )
    lease = await recovered.claim(event)
    assert lease.owner_token is not None
    assert await recovered.complete(event, lease) is ApprovalDeliveryResult.DELIVERED


@pytest.mark.asyncio
async def test_concurrent_delivery_non_owner_requests_outbox_retry_not_ack() -> None:
    event = ApprovalNotification(
        event_id="evt-concurrent",
        approval_id="approval-1",
        status="pending",
        attempts=1,
    )
    backend = FakeBackend()
    first = DurableApprovalDelivery(backend, sink_id="gateway-test")
    second = DurableApprovalDelivery(backend, sink_id="gateway-test")

    first_lease, second_lease = await asyncio.gather(
        first.claim(event), second.claim(event)
    )

    assert sum(lease.owner_token is not None for lease in (first_lease, second_lease)) == 1
    assert sum(lease.result is ApprovalDeliveryResult.RETRY for lease in (first_lease, second_lease)) == 1
    duplicate = next(lease for lease in (first_lease, second_lease) if lease.owner_token is None)
    assert await first.complete(event, duplicate) is ApprovalDeliveryResult.RETRY
    assert (await first.records())[0]["state"] == "inflight"


@pytest.mark.asyncio
async def test_stale_owner_cannot_complete_after_an_expired_lease_is_reclaimed() -> None:
    event = ApprovalNotification(
        event_id="evt-stale-owner",
        approval_id="approval-1",
        status="pending",
        attempts=1,
    )
    backend = FakeBackend()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    original = DurableApprovalDelivery(
        backend, sink_id="gateway-test", clock=lambda: now, lease_seconds=30
    )
    original_lease = await original.claim(event)
    recovered = DurableApprovalDelivery(
        backend,
        sink_id="gateway-test",
        clock=lambda: now + timedelta(seconds=31),
        lease_seconds=30,
    )
    recovered_lease = await recovered.claim(event)

    assert await original.complete(event, original_lease) is ApprovalDeliveryResult.RETRY
    assert await recovered.complete(event, recovered_lease) is ApprovalDeliveryResult.DELIVERED


@pytest.mark.asyncio
async def test_inflight_gateway_claim_nacks_outbox_without_sending_or_acknowledging() -> None:
    event = ApprovalNotification(
        event_id="evt-inflight",
        approval_id="approval-1",
        status="pending",
        attempts=1,
    )
    backend = FakeBackend()
    await backend.mutable_write_with_outbox(
        "approvals",
        event.approval_id,
        {"status": event.status},
        event_id=event.event_id,
        event={"approval_id": event.approval_id, "status": event.status},
        actor_did="did:arc:test",
    )
    target = DeliveryTarget.parse("web:operator")
    owner = DurableApprovalDelivery(backend, sink_id="gateway-web-operator-")
    assert (await owner.claim(event)).owner_token is not None
    router = _Router()
    dispatcher = ApprovalNotificationDispatcher(
        backend,
        GatewayApprovalNotificationSink(router, target, backend=backend),
        ApprovalDispatcherConfig(worker_id="worker-a", retry_base_seconds=0),
    )

    await dispatcher.dispatch_once()

    assert router.calls == []
    assert [row["event_id"] for row in await backend.claim_outbox("worker-b")] == [event.event_id]
