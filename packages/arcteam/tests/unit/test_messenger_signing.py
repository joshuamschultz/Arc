"""Signed-envelope messaging: sign on send, verify + replay on consume (REQ-030/031)."""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, patch

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
    await svc.create_channel(Channel(name="ops", members=["agent://a1", "agent://a2"]))
    return svc, registry, signer


class TestSigningOnSend:
    async def test_send_signs_message(self) -> None:
        svc, _, _ = await _service_with_signer()
        sent = await svc.send(Message(sender="agent://a1", to=["agent://a2"], body="hi"))
        assert sent.sig != ""
        assert sent.nonce != ""
        assert sent.signer_did == DID_A1

    async def test_lookup_proves_exact_signed_reply_after_lost_response(self) -> None:
        svc, _, _ = await _service_with_signer()
        sent = await svc.send(
            Message(id="reply_1", sender="agent://a1", to=["channel://ops"], body="answer")
        )
        digest = hashlib.sha256(b"answer").hexdigest()
        assert await svc.find_sent(
            message_id=sent.id, target="channel://ops",
            body_digest=digest,
        )
        assert not await svc.find_sent(
            message_id=sent.id, target="channel://ops",
            body_digest=hashlib.sha256(b"forged").hexdigest(),
        )

    async def test_lookup_refuses_tampered_or_duplicate_receipt(self) -> None:
        svc, _, _ = await _service_with_signer()
        sent = await svc.send(
            Message(id="reply_1", sender="agent://a1", to=["channel://ops"], body="answer")
        )
        digest = hashlib.sha256(b"answer").hexdigest()
        records = await svc._backend.read_stream("messages/streams", "arc.channel.ops")
        records[0]["body"] = "tampered"
        assert not await svc.find_sent(
            message_id=sent.id, target="channel://ops",
            body_digest=digest,
        )
        records[0]["body"] = "answer"
        assert await svc.find_sent(
            message_id=sent.id, target="channel://ops",
            body_digest=digest,
        )
        await svc._backend.append_auto_seq("messages/streams", "arc.channel.ops", records[0].copy())
        assert not await svc.find_sent(
            message_id=sent.id, target="channel://ops",
            body_digest=digest,
        )

    async def test_lookup_refuses_foreign_channel_and_forged_signer_before_history_read(self) -> None:
        svc, _, signer = await _service_with_signer()
        await svc.create_channel(Channel(name="private", members=["agent://a2"]))
        digest = hashlib.sha256(b"answer").hexdigest()
        with patch.object(svc._backend, "read_stream", side_effect=AssertionError("history read")):
            assert not await svc.find_sent(
                message_id="reply_1", target="channel://private", body_digest=digest,
            )
            svc._signer = MessageSigner(did=signer.did, private_key=bytes(range(32)))
            assert not await svc.find_sent(
                message_id="reply_1", target="channel://ops", body_digest=digest,
            )

    async def test_lookup_fails_closed_before_history_read_when_audit_fails(self) -> None:
        svc, _, _ = await _service_with_signer()
        digest = hashlib.sha256(b"answer").hexdigest()
        with patch.object(svc._backend, "read_stream", side_effect=AssertionError("history read")):
            with patch.object(svc._audit, "log", new=AsyncMock(side_effect=RuntimeError("audit down"))):
                with pytest.raises(RuntimeError, match="audit down"):
                    await svc.find_sent(
                        message_id="reply_1", target="channel://ops", body_digest=digest,
                    )


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
