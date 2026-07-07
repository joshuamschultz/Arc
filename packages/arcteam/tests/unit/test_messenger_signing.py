"""Signed-envelope messaging: sign on send, verify + replay on consume (REQ-030/031)."""

from __future__ import annotations

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


async def _service_with_signer() -> tuple[MessagingService, EntityRegistry, MessageSigner]:
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
        Entity(
            did="did:arc:local:agent/a2",
            handle="a2",
            id="agent://a2",
            name="A2",
            type=EntityType.AGENT,
        )
    )
    signer = MessageSigner(did=DID_A1, private_key=kp.private_key)
    svc = MessagingService(backend, registry, audit, signer=signer)
    return svc, registry, signer


class TestSigningOnSend:
    async def test_send_signs_message(self) -> None:
        svc, _, _ = await _service_with_signer()
        sent = await svc.send(Message(sender="agent://a1", to=["agent://a2"], body="hi"))
        assert sent.sig != ""
        assert sent.nonce != ""
        assert sent.signer_did == DID_A1


class TestVerifyOnConsume:
    async def test_valid_message_delivered(self) -> None:
        svc, _, _ = await _service_with_signer()
        await svc.send(Message(sender="agent://a1", to=["agent://a2"], body="hi"))
        received = await svc.receive("arc.agent.a2", "agent://a2")
        assert [m.body for m in received] == ["hi"]

    async def test_tampered_message_goes_to_dlq(self) -> None:
        svc, _, _ = await _service_with_signer()
        await svc.send(Message(sender="agent://a1", to=["agent://a2"], body="hi"))
        # Tamper with the stored record in place (simulate a corrupted wire).
        stored = await svc._backend.read_stream("messages/streams", "arc.agent.a2")
        stored[0]["body"] = "tampered"
        received = await svc.receive("arc.agent.a2", "agent://a2")
        assert received == []
        dlq = await svc.dlq_list()
        assert any(e["meta"].get("dlq_reason") == "bad_signature" for e in dlq)

    async def test_replayed_message_goes_to_dlq(self) -> None:
        svc, _, _ = await _service_with_signer()
        await svc.send(Message(sender="agent://a1", to=["agent://a2"], body="hi"))
        first = await svc.receive("arc.agent.a2", "agent://a2")
        assert len(first) == 1
        # Re-consuming the very same nonce is a replay.
        second = await svc.receive("arc.agent.a2", "agent://a2")
        assert second == []
        dlq = await svc.dlq_list()
        assert any(e["meta"].get("dlq_reason") == "replay" for e in dlq)


class TestUnsignedSendIsRejectedOnConsume:
    """A signer-less service produces unsigned envelopes, and verification is
    unconditional (SPEC-031 review, FIX 3): an unsigned message can never be
    delivered — it is quarantined as ``bad_signature``. This replaces the old
    ``test_no_signer_leaves_messages_unsigned`` which encoded the pre-fix bug
    that a keyless receiver accepted unsigned traffic."""

    async def test_unsigned_message_never_delivered(self) -> None:
        backend = MemoryBackend()
        audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
        await audit.initialize()
        registry = EntityRegistry(backend, audit)
        await registry.register(
            Entity(did=DID_A1, handle="a1", id="agent://a1", name="A1", type=EntityType.AGENT)
        )
        await registry.register(
            Entity(
                did="did:arc:local:agent/a2",
                handle="a2",
                id="agent://a2",
                name="A2",
                type=EntityType.AGENT,
            )
        )
        svc = MessagingService(backend, registry, audit)
        sent = await svc.send(Message(sender="agent://a1", to=["agent://a2"], body="hi"))
        assert sent.sig == ""
        received = await svc.receive("arc.agent.a2", "agent://a2")
        assert received == []
        dlq = await svc.dlq_list()
        assert any(e["meta"].get("dlq_reason") == "bad_signature" for e in dlq)
