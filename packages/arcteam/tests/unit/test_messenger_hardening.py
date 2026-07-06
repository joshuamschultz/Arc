"""Security/correctness hardening of the verify path (SPEC-031 review).

Covers four findings:
  * origin forgery — a peer must not publish ``sender=agent://alice`` under its
    own signer and have it accepted as from alice (bad_signature).
  * unconditional verification — a keyless/verify-only receiver still verifies
    incoming messages and rejects forged/unsigned ones.
  * signer trust — an unregistered signer_did or one lacking a public key is
    rejected (bad_signature).
  * fan-out dedup — one message written to two streams the same entity consumes
    is delivered exactly once, NOT quarantined as a replay.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from arctrust import generate_keypair

from arcteam.audit import AuditLogger
from arcteam.crypto import MessageSigner
from arcteam.messenger import MessagingService
from arcteam.registry import EntityRegistry
from arcteam.storage import Consumer, MemoryBackend, StorageBackend
from arcteam.types import Channel, Entity, EntityType, Message

pytestmark = pytest.mark.asyncio

STREAMS_COLLECTION = "messages/streams"


async def _bootstrap() -> tuple[MemoryBackend, EntityRegistry, AuditLogger]:
    backend = MemoryBackend()
    audit = AuditLogger(backend, hmac_key=b"k" * 32)
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    return backend, registry, audit


async def _register(
    registry: EntityRegistry,
    handle: str,
    *,
    public_key: str | None = None,
    roles: list[str] | None = None,
) -> None:
    await registry.register(
        Entity(
            did=f"did:arc:local:agent/{handle}",
            handle=handle,
            id=f"agent://{handle}",
            name=handle.upper(),
            type=EntityType.AGENT,
            public_key=public_key or "",
            roles=roles or [],
        )
    )


# ---------------------------------------------------------------------------
# FIX 2 — origin forgery
# ---------------------------------------------------------------------------


class TestForgedSenderRejected:
    async def test_peer_cannot_spoof_another_sender(self) -> None:
        """Mallory signs with her own key but claims sender=agent://alice."""
        backend, registry, audit = await _bootstrap()
        alice_kp = generate_keypair()
        mallory_kp = generate_keypair()
        await _register(registry, "alice", public_key=alice_kp.public_key.hex())
        await _register(registry, "mallory", public_key=mallory_kp.public_key.hex())
        await _register(registry, "bob")

        # Mallory's service: it signs every send with mallory's key.
        mallory_svc = MessagingService(
            backend, registry, audit, signer=MessageSigner("did:arc:local:agent/mallory", mallory_kp.private_key)
        )
        # She forges the sender field.
        await mallory_svc.send(Message(sender="agent://alice", to=["agent://bob"], body="pwn"))

        bob_svc = MessagingService(backend, registry, audit)
        received = await bob_svc.receive("arc.agent.bob", "agent://bob")
        assert received == []
        dlq = await bob_svc.dlq_list()
        assert any(e["meta"].get("dlq_reason") == "bad_signature" for e in dlq)

    async def test_legitimate_send_still_passes(self) -> None:
        """sender resolves to the same DID as signer_did — must be delivered."""
        backend, registry, audit = await _bootstrap()
        alice_kp = generate_keypair()
        await _register(registry, "alice", public_key=alice_kp.public_key.hex())
        await _register(registry, "bob")
        svc = MessagingService(
            backend, registry, audit, signer=MessageSigner("did:arc:local:agent/alice", alice_kp.private_key)
        )
        await svc.send(Message(sender="agent://alice", to=["agent://bob"], body="hi"))
        received = await svc.receive("arc.agent.bob", "agent://bob")
        assert [m.body for m in received] == ["hi"]


# ---------------------------------------------------------------------------
# FIX 3 — verification is unconditional (keyless receiver still verifies)
# ---------------------------------------------------------------------------


class TestKeylessReceiverVerifies:
    async def test_keyless_receiver_rejects_forged_and_keeps_valid(self) -> None:
        backend, registry, audit = await _bootstrap()
        alice_kp = generate_keypair()
        await _register(registry, "alice", public_key=alice_kp.public_key.hex())
        await _register(registry, "bob")
        alice_svc = MessagingService(
            backend, registry, audit, signer=MessageSigner("did:arc:local:agent/alice", alice_kp.private_key)
        )
        await alice_svc.send(Message(sender="agent://alice", to=["agent://bob"], body="ok"))
        await alice_svc.send(Message(sender="agent://alice", to=["agent://bob"], body="evil"))
        # Tamper the second record on the wire.
        stored = await backend.read_stream(STREAMS_COLLECTION, "arc.agent.bob")
        stored[1]["body"] = "tampered"

        # Receiver holds NO signer, yet must still verify.
        keyless = MessagingService(backend, registry, audit)
        received = await keyless.receive("arc.agent.bob", "agent://bob")
        assert [m.body for m in received] == ["ok"]
        dlq = await keyless.dlq_list()
        assert any(e["meta"].get("dlq_reason") == "bad_signature" for e in dlq)

    async def test_keyless_receiver_rejects_unsigned(self) -> None:
        backend, registry, audit = await _bootstrap()
        await _register(registry, "alice")
        await _register(registry, "bob")
        # Unsigned sender (no signer) writes a bare message.
        unsigned_svc = MessagingService(backend, registry, audit)
        await unsigned_svc.send(Message(sender="agent://alice", to=["agent://bob"], body="plain"))

        keyless = MessagingService(backend, registry, audit)
        received = await keyless.receive("arc.agent.bob", "agent://bob")
        assert received == []
        dlq = await keyless.dlq_list()
        assert any(e["meta"].get("dlq_reason") == "bad_signature" for e in dlq)


class TestSignerTrust:
    async def test_unregistered_signer_did_rejected(self) -> None:
        backend, registry, audit = await _bootstrap()
        alice_kp = generate_keypair()
        await _register(registry, "alice", public_key=alice_kp.public_key.hex())
        await _register(registry, "bob")
        svc = MessagingService(
            backend, registry, audit, signer=MessageSigner("did:arc:local:agent/alice", alice_kp.private_key)
        )
        await svc.send(Message(sender="agent://alice", to=["agent://bob"], body="hi"))
        # Rewrite signer_did to an entity that is not registered.
        stored = await backend.read_stream(STREAMS_COLLECTION, "arc.agent.bob")
        stored[0]["signer_did"] = "did:arc:local:agent/ghost"
        received = await svc.receive("arc.agent.bob", "agent://bob")
        assert received == []
        dlq = await svc.dlq_list()
        assert any(e["meta"].get("dlq_reason") == "bad_signature" for e in dlq)

    async def test_signer_without_public_key_rejected(self) -> None:
        backend, registry, audit = await _bootstrap()
        alice_kp = generate_keypair()
        # Alice registered WITHOUT a public key.
        await _register(registry, "alice")
        await _register(registry, "bob")
        svc = MessagingService(
            backend, registry, audit, signer=MessageSigner("did:arc:local:agent/alice", alice_kp.private_key)
        )
        await svc.send(Message(sender="agent://alice", to=["agent://bob"], body="hi"))
        received = await svc.receive("arc.agent.bob", "agent://bob")
        assert received == []
        dlq = await svc.dlq_list()
        assert any(e["meta"].get("dlq_reason") == "bad_signature" for e in dlq)


# ---------------------------------------------------------------------------
# FIX 4 — benign fan-out is a duplicate, not a replay
# ---------------------------------------------------------------------------


class TestFanOutDedup:
    async def test_multi_target_to_one_entity_delivered_once(self) -> None:
        backend, registry, audit = await _bootstrap()
        alice_kp = generate_keypair()
        await _register(registry, "alice", public_key=alice_kp.public_key.hex())
        await _register(registry, "builder", roles=["dev"])
        svc = MessagingService(
            backend, registry, audit, signer=MessageSigner("did:arc:local:agent/alice", alice_kp.private_key)
        )

        received: list[Message] = []
        got = asyncio.Event()

        async def handler(message: Message) -> None:
            received.append(message)
            got.set()

        subscription = await svc.subscribe("agent://builder", handler)
        try:
            # builder is a target directly AND via role://dev — one message,
            # written to two streams builder consumes.
            await svc.send(
                Message(sender="agent://alice", to=["@builder", "role://dev"], body="one")
            )
            await asyncio.wait_for(got.wait(), timeout=3)
            await asyncio.sleep(0.2)  # give the second stream a chance to double-deliver
        finally:
            await subscription.stop()

        assert [m.body for m in received] == ["one"]
        dlq = await svc.dlq_list()
        assert not any(e["meta"].get("dlq_reason") == "replay" for e in dlq)


# ---------------------------------------------------------------------------
# Consume-loop resilience
# ---------------------------------------------------------------------------


class _FlakyConsumer:
    """Wraps a real consumer; raises once on first fetch, then delegates."""

    def __init__(self, inner: Consumer) -> None:
        self._inner = inner
        self._raised = False

    async def fetch(self, batch: int) -> list:
        if not self._raised:
            self._raised = True
            raise RuntimeError("transient fetch failure")
        return await self._inner.fetch(batch)


class _FlakyBackend:
    """Delegates to a MemoryBackend but hands out a flaky consumer once."""

    def __init__(self, inner: MemoryBackend) -> None:
        self._inner = inner
        self._wrapped = False

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._inner, name)

    async def open_consumer(self, collection: str, key: str, durable: str) -> Consumer:
        inner = await self._inner.open_consumer(collection, key, durable)
        if not self._wrapped:
            self._wrapped = True
            return _FlakyConsumer(inner)
        return inner


class TestConsumeLoopResilience:
    async def test_handler_exception_is_acked_and_loop_survives(self) -> None:
        backend, registry, audit = await _bootstrap()
        alice_kp = generate_keypair()
        await _register(registry, "alice", public_key=alice_kp.public_key.hex())
        await _register(registry, "bob")
        svc = MessagingService(
            backend, registry, audit, signer=MessageSigner("did:arc:local:agent/alice", alice_kp.private_key)
        )

        seen: list[str] = []
        second = asyncio.Event()

        async def handler(message: Message) -> None:
            if message.body == "boom":
                raise RuntimeError("handler blew up")
            seen.append(message.body)
            second.set()

        subscription = await svc.subscribe("agent://bob", handler)
        try:
            await svc.send(Message(sender="agent://alice", to=["agent://bob"], body="boom"))
            await asyncio.sleep(0.2)
            await svc.send(Message(sender="agent://alice", to=["agent://bob"], body="after"))
            await asyncio.wait_for(second.wait(), timeout=3)
        finally:
            await subscription.stop()

        assert seen == ["after"]

    async def test_fetch_exception_recovers_and_delivers(self) -> None:
        backend, registry, audit = await _bootstrap()
        alice_kp = generate_keypair()
        await _register(registry, "alice", public_key=alice_kp.public_key.hex())
        await _register(registry, "bob")
        flaky: StorageBackend = _FlakyBackend(backend)  # type: ignore[assignment]
        svc = MessagingService(
            flaky, registry, audit, signer=MessageSigner("did:arc:local:agent/alice", alice_kp.private_key)
        )

        received: list[str] = []
        got = asyncio.Event()

        async def handler(message: Message) -> None:
            received.append(message.body)
            got.set()

        subscription = await svc.subscribe("agent://bob", handler)
        try:
            await svc.send(Message(sender="agent://alice", to=["agent://bob"], body="survivor"))
            await asyncio.wait_for(got.wait(), timeout=3)
        finally:
            await subscription.stop()

        assert received == ["survivor"]


# ---------------------------------------------------------------------------
# Channel push delivery
# ---------------------------------------------------------------------------


class TestChannelPush:
    async def test_member_receives_channel_message(self) -> None:
        backend, registry, audit = await _bootstrap()
        alice_kp = generate_keypair()
        await _register(registry, "alice", public_key=alice_kp.public_key.hex())
        await _register(registry, "builder")
        svc = MessagingService(
            backend, registry, audit, signer=MessageSigner("did:arc:local:agent/alice", alice_kp.private_key)
        )
        await svc.create_channel(
            Channel(name="ops", members=["agent://alice", "agent://builder"])
        )

        received: list[str] = []
        got = asyncio.Event()

        async def handler(message: Message) -> None:
            received.append(message.body)
            got.set()

        subscription = await svc.subscribe("agent://builder", handler)
        try:
            await svc.send(Message(sender="agent://alice", to=["channel://ops"], body="team"))
            await asyncio.wait_for(got.wait(), timeout=3)
        finally:
            await subscription.stop()

        assert received == ["team"]


class TestSubscriptionAddressForms:
    """Every address form resolves to the SAME inbox stream ``send`` writes to.

    Regression: a poll for ``@builder`` must not listen on ``arc.agent.@builder``
    while ``send`` routed to ``arc.agent.builder`` — the ``@handle`` form has to
    normalize on the subscription side too.
    """

    async def test_handle_forms_map_to_the_send_stream(self) -> None:
        backend, registry, audit = await _bootstrap()
        svc = MessagingService(backend, registry, audit)
        assert svc.resolve_subscriptions("@builder")[0] == "arc.agent.builder"
        assert svc.resolve_subscriptions("@builder") == svc.resolve_subscriptions("agent://builder")
        assert svc.resolve_subscriptions("builder")[0] == "arc.agent.builder"
        assert "arc.role.dev" in svc.resolve_subscriptions("@builder", ["dev"])


@dataclass
class _RecordingDelivery:
    """Fake Delivery that records whether it was acked (FIX #5)."""

    data: dict[str, Any]
    acked: bool = False

    async def ack(self) -> None:
        self.acked = True


class TestRetryableDelivery:
    """FIX #5 — a handler signalling backpressure defers redelivery, never drops."""

    async def test_retryable_error_is_not_acked_and_redelivers(self) -> None:
        from arcteam.messenger import RetryableDeliveryError

        backend, registry, audit = await _bootstrap()
        alice_kp = generate_keypair()
        await _register(registry, "alice", public_key=alice_kp.public_key.hex())
        await _register(registry, "bob")
        svc = MessagingService(
            backend,
            registry,
            audit,
            signer=MessageSigner("did:arc:local:agent/alice", alice_kp.private_key),
        )
        sent = await svc.send(Message(sender="agent://alice", to=["agent://bob"], body="x"))
        seen: set[str] = set()

        async def backpressured(_m: Message) -> None:
            raise RetryableDeliveryError("steering queue full")

        first = _RecordingDelivery(data=sent.model_dump())
        await svc._dispatch(first, backpressured, seen)
        # Not acked and not marked delivered → the durable consumer redelivers it.
        assert first.acked is False
        assert sent.id not in seen

        got: list[str] = []

        async def accept(m: Message) -> None:
            got.append(m.body)

        second = _RecordingDelivery(data=sent.model_dump())
        await svc._dispatch(second, accept, seen)
        # Redelivery is accepted exactly once, then acked.
        assert second.acked is True
        assert got == ["x"]
        assert sent.id in seen
