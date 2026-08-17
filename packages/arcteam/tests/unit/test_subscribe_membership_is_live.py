"""Channel membership is a live property of a subscription, not a boot snapshot.

SPEC-068 F2. ``MessagingService.subscribe`` resolved the entity's member
channels once, inside the call, and then blocked forever on the consume loops.
A channel created after an agent subscribed — or an agent added to a channel
after it subscribed — delivered nothing at all until that agent restarted.

The dashboard makes this reachable in one click: an operator adds an agent to a
channel through the members sheet and posts, and the running agent never sees
it. Nothing logs an error, because nothing is wrong on the send side; the
message lands on a stream the agent is not consuming.

These tests pin the re-resolve. They drive the loop directly rather than
sleeping through its real interval, so they neither sleep for seconds nor race.
"""

from __future__ import annotations

import asyncio

import pytest
from arctrust import generate_keypair
from arctrust.signer import InProcessSigner

from arcteam.audit import AuditLogger
from arcteam.crypto import MessageSigner
from arcteam.messenger import MessagingService
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Channel, Entity, EntityType, Message

pytestmark = pytest.mark.asyncio

DID_SENDER = "did:arc:local:agent/sender"
DID_LATE = "did:arc:local:agent/late"


async def _service() -> MessagingService:
    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    kp = generate_keypair()
    await registry.register(
        Entity(
            did=DID_SENDER,
            handle="sender",
            id="agent://sender",
            name="Sender",
            type=EntityType.AGENT,
            public_key=kp.public_key.hex(),
        )
    )
    await registry.register(
        Entity(
            did=DID_LATE,
            handle="late",
            id="agent://late",
            name="Late",
            type=EntityType.AGENT,
        )
    )
    return MessagingService(
        backend, registry, audit, signer=MessageSigner(DID_SENDER, kp.private_key)
    )


async def test_channel_joined_after_subscribe_is_delivered() -> None:
    """An agent added to a channel while running receives that channel's posts."""
    svc = await _service()
    received: list[Message] = []
    delivered = asyncio.Event()

    async def handler(message: Message) -> None:
        received.append(message)
        delivered.set()

    # Subscribes owning no channel at all — the boot-time snapshot is empty.
    subscription = await svc.subscribe("agent://late", handler)
    try:
        await svc.create_channel(Channel(name="work", members=[DID_SENDER]))
        await svc.join_channel("work", DID_LATE)

        # What the supervisor does on its timer, without waiting for the timer.
        await svc.refresh_subscription("agent://late", subscription)

        await svc.send(Message(sender="agent://sender", to=["channel://work"], body="hello"))
        await asyncio.wait_for(delivered.wait(), timeout=3)
    finally:
        await subscription.stop()

    assert [m.body for m in received] == ["hello"]


async def test_refresh_is_idempotent_and_opens_no_duplicate_consumer() -> None:
    """Refreshing twice must not double-deliver a channel already subscribed."""
    svc = await _service()
    received: list[Message] = []

    async def handler(message: Message) -> None:
        received.append(message)

    await svc.create_channel(Channel(name="ops", members=[DID_SENDER, DID_LATE]))
    subscription = await svc.subscribe("agent://late", handler)
    try:
        await svc.refresh_subscription("agent://late", subscription)
        await svc.refresh_subscription("agent://late", subscription)

        await svc.send(Message(sender="agent://sender", to=["channel://ops"], body="once"))
        await asyncio.sleep(0.4)
    finally:
        await subscription.stop()

    assert [m.body for m in received] == ["once"]


async def test_stop_cancels_the_resubscribe_supervisor() -> None:
    """``stop`` leaves no task behind — including the one added after construction."""
    svc = await _service()

    async def handler(_message: Message) -> None:  # pragma: no cover - never called
        raise AssertionError("no message was sent")

    subscription = await svc.subscribe("agent://late", handler)
    tasks = list(subscription.tasks)
    assert any("resubscribe" in (t.get_name() or "") for t in tasks), (
        "subscribe must start a supervisor that re-resolves membership"
    )

    await subscription.stop()
    assert all(t.done() for t in tasks)
