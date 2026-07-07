"""Durable PUSH subscribe: live delivery + verify/replay + resume-from-ack (REQ-021).

``MessagingService.subscribe`` runs a background consume-loop over a durable
consumer — the modern push model. A running subscriber receives a bus-pushed
message with no interval poll from the caller; every delivery passes the same
Ed25519 verify + replay check as ``receive`` (invalid -> DLQ), and the ack floor
lets a re-subscribe resume with zero missed or duplicated messages.
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
from arcteam.types import Entity, EntityType, Message

pytestmark = pytest.mark.asyncio

DID_A1 = "did:arc:local:agent/a1"
DID_A2 = "did:arc:local:agent/a2"


async def _signed_service() -> MessagingService:
    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    kp = generate_keypair()
    await registry.register(
        Entity(
            did=DID_A1,
            handle="a1",
            id="agent://a1",
            name="A1",
            type=EntityType.AGENT,
            public_key=kp.public_key.hex(),
        )
    )
    await registry.register(
        Entity(did=DID_A2, handle="a2", id="agent://a2", name="A2", type=EntityType.AGENT)
    )
    return MessagingService(backend, registry, audit, signer=MessageSigner(DID_A1, kp.private_key))


async def test_running_subscriber_receives_pushed_message() -> None:
    """A subscribed handler receives a bus-pushed message — the caller never polls."""
    svc = await _signed_service()
    received: list[Message] = []
    delivered = asyncio.Event()

    async def handler(message: Message) -> None:
        received.append(message)
        delivered.set()

    subscription = await svc.subscribe("agent://a2", handler)
    try:
        # Consumer is already running; the send pushes to it live.
        await svc.send(Message(sender="agent://a1", to=["agent://a2"], body="live"))
        await asyncio.wait_for(delivered.wait(), timeout=3)
    finally:
        await subscription.stop()

    assert [m.body for m in received] == ["live"]


async def test_subscribe_verifies_and_dlqs_tampered() -> None:
    """A tampered message is quarantined to the DLQ, never handed to the handler."""
    svc = await _signed_service()
    received: list[Message] = []

    async def handler(message: Message) -> None:
        received.append(message)

    await svc.send(Message(sender="agent://a1", to=["agent://a2"], body="hi"))
    stored = await svc._backend.read_stream("messages/streams", "arc.agent.a2")
    stored[0]["body"] = "tampered"

    subscription = await svc.subscribe("agent://a2", handler)
    try:
        await asyncio.sleep(0.3)
    finally:
        await subscription.stop()

    assert received == []
    dlq = await svc.dlq_list()
    assert any(e["meta"].get("dlq_reason") == "bad_signature" for e in dlq)


async def test_subscribe_acks_enable_resume() -> None:
    """After ack, a re-subscribe resumes from the floor: no missed, no duplicated."""
    svc = await _signed_service()

    first: list[Message] = []
    got_first = asyncio.Event()

    async def handler_one(message: Message) -> None:
        first.append(message)
        got_first.set()

    await svc.send(Message(sender="agent://a1", to=["agent://a2"], body="one"))
    sub1 = await svc.subscribe("agent://a2", handler_one)
    try:
        await asyncio.wait_for(got_first.wait(), timeout=3)
    finally:
        await sub1.stop()
    assert [m.body for m in first] == ["one"]

    # Re-subscribe with the same entity (same durable). "one" was acked, so it
    # must not be redelivered; a fresh send arrives.
    second: list[Message] = []
    got_second = asyncio.Event()

    async def handler_two(message: Message) -> None:
        second.append(message)
        got_second.set()

    sub2 = await svc.subscribe("agent://a2", handler_two)
    try:
        await svc.send(Message(sender="agent://a1", to=["agent://a2"], body="two"))
        await asyncio.wait_for(got_second.wait(), timeout=3)
    finally:
        await sub2.stop()

    assert [m.body for m in second] == ["two"]


async def test_memory_backend_open_consumer_resumes_from_ack() -> None:
    """MemoryBackend durable consumer mirrors JetStream resume semantics."""
    backend = MemoryBackend()
    for i in range(3):
        await backend.append_auto_seq("messages/streams", "arc.agent.a1", {"body": str(i)})

    consumer = await backend.open_consumer("messages/streams", "arc.agent.a1", "a1-inbox")
    first = await consumer.fetch(2)
    assert [m.data["body"] for m in first] == ["0", "1"]
    for m in first:
        await m.ack()

    resumed = await backend.open_consumer("messages/streams", "arc.agent.a1", "a1-inbox")
    rest = await resumed.fetch(10)
    assert [m.data["body"] for m in rest] == ["2"]


@pytest.mark.slow
async def test_subscribe_pushes_over_real_nats() -> None:
    """The same push path works over a real nats-server with JetStream (REQ-020/021)."""
    import shutil
    import socket
    import subprocess
    import tempfile
    import time
    from pathlib import Path

    if shutil.which("nats-server") is None:
        pytest.skip("nats-server not installed")

    from arcteam.backends.nats import NatsBackend

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    store = tempfile.mkdtemp(prefix="arcteam-sub-")
    proc = subprocess.Popen(
        ["nats-server", "-js", "-p", str(port), "-sd", store],  # noqa: S607
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        proc.terminate()
        pytest.fail("nats-server did not start")

    backend = await NatsBackend.connect(f"nats://127.0.0.1:{port}")
    try:
        audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
        await audit.initialize()
        registry = EntityRegistry(backend, audit)
        kp = generate_keypair()
        await registry.register(
            Entity(
                did=DID_A1,
                handle="a1",
                id="agent://a1",
                name="A1",
                type=EntityType.AGENT,
                public_key=kp.public_key.hex(),
            )
        )
        await registry.register(
            Entity(did=DID_A2, handle="a2", id="agent://a2", name="A2", type=EntityType.AGENT)
        )
        svc = MessagingService(
            backend, registry, audit, signer=MessageSigner(DID_A1, kp.private_key)
        )

        received: list[Message] = []
        delivered = asyncio.Event()

        async def handler(message: Message) -> None:
            received.append(message)
            delivered.set()

        subscription = await svc.subscribe("agent://a2", handler)
        try:
            await svc.send(Message(sender="agent://a1", to=["agent://a2"], body="live"))
            await asyncio.wait_for(delivered.wait(), timeout=10)
        finally:
            await subscription.stop()

        assert [m.body for m in received] == ["live"]
    finally:
        await backend.close()
        proc.terminate()
        proc.wait(timeout=5)
        shutil.rmtree(Path(store), ignore_errors=True)
