"""Tests for arcteam.audit — AuditLogger with a chained asymmetric signature.

SPEC-037 REQ-002: each record is signed with an arctrust Ed25519 ``Signer``
over ``prev_signature || canonical(record)``; ``verify_chain`` verifies against
the operator public key, and any record/signature mutation fails.
"""

from __future__ import annotations

import pytest
from arctrust.keypair import generate_keypair
from arctrust.signer import InProcessSigner

from arcteam.audit import AuditLogger
from arcteam.storage import MemoryBackend


def _signer() -> InProcessSigner:
    return InProcessSigner(generate_keypair().private_key)


@pytest.fixture
async def audit() -> AuditLogger:
    backend = MemoryBackend()
    al = AuditLogger(backend, _signer())
    await al.initialize()
    return al


@pytest.fixture
def backend() -> MemoryBackend:
    return MemoryBackend()


class TestSingleRecord:
    async def test_log_creates_signed_record(self, audit: AuditLogger) -> None:
        await audit.log(
            event_type="message.sent",
            subject="arc.channel.ops",
            actor_id="agent://a1",
            detail="sent message to channel",
            stream="arc.channel.ops",
            msg_seq=1,
        )
        records = await audit._backend.read_stream("audit", "audit", after_seq=0)
        assert len(records) == 1
        r = records[0]
        assert r["audit_seq"] == 1
        assert r["event_type"] == "message.sent"
        assert r["actor_id"] == "agent://a1"
        # Asymmetric signature triad present; no HMAC field.
        assert r["signature"] != ""
        assert r["algorithm"] == "ed25519"
        assert r["public_key"] != ""
        assert "hmac_sha256" not in r


class TestChainIntegrity:
    async def test_valid_chain_ed25519(self, audit: AuditLogger) -> None:
        for i in range(10):
            await audit.log(
                event_type=f"event_{i}",
                subject="test",
                actor_id="agent://a1",
                detail=f"record {i}",
            )
        valid, last_seq = await audit.verify_chain()
        assert valid is True
        assert last_seq == 10

    async def test_chain_verifies_against_operator_public_key(
        self, backend: MemoryBackend
    ) -> None:
        """The signature verifies with the public key ONLY (non-repudiation)."""
        from arctrust.signer import ED25519, verify_signature

        from arcteam.audit import _signing_input

        signer = _signer()
        audit = AuditLogger(backend, signer)
        await audit.initialize()
        await audit.log(event_type="e", subject="s", actor_id="agent://a1", detail="d")

        record = backend._streams["audit"]["audit"][0]
        signature = bytes.fromhex(record["signature"])
        assert verify_signature(
            ED25519, _signing_input(record, ""), signature, signer.public_key
        )


class TestTamperDetection:
    async def test_tampered_record_fails(self, backend: MemoryBackend) -> None:
        audit = AuditLogger(backend, _signer())
        await audit.initialize()
        for i in range(5):
            await audit.log(
                event_type=f"event_{i}",
                subject="test",
                actor_id="agent://a1",
                detail=f"record {i}",
            )

        backend._streams["audit"]["audit"][2]["detail"] = "TAMPERED"

        valid, last_seq = await audit.verify_chain()
        assert valid is False
        assert last_seq == 2  # verified up to the record before tampering

    async def test_forged_signature_fails(self, backend: MemoryBackend) -> None:
        """A record re-signed under a DIFFERENT key must fail verification —
        the verifier trusts the operator key, not the record's embedded key."""
        audit = AuditLogger(backend, _signer())
        await audit.initialize()
        for i in range(3):
            await audit.log(
                event_type=f"event_{i}",
                subject="test",
                actor_id="agent://a1",
                detail=f"record {i}",
            )

        # Attacker re-signs record[1] with their own key and swaps the pubkey in.
        from arcteam.audit import _signing_input

        attacker = _signer()
        rec = backend._streams["audit"]["audit"][1]
        prev_sig = backend._streams["audit"]["audit"][0]["signature"]
        rec["signature"] = attacker.sign(_signing_input(rec, prev_sig)).hex()
        rec["public_key"] = attacker.public_key.hex()

        valid, last_seq = await audit.verify_chain()
        assert valid is False
        assert last_seq == 1


class TestGapDetection:
    async def test_sequence_gap_fails(self, backend: MemoryBackend) -> None:
        audit = AuditLogger(backend, _signer())
        await audit.initialize()
        for i in range(5):
            await audit.log(
                event_type=f"event_{i}",
                subject="test",
                actor_id="agent://a1",
                detail=f"record {i}",
            )

        del backend._streams["audit"]["audit"][2]

        valid, last_seq = await audit.verify_chain()
        assert valid is False
        assert last_seq == 2


class TestPersistence:
    async def test_chain_resumes_across_instances(self, backend: MemoryBackend) -> None:
        """A second logger over the SAME signer + backend continues the chain."""
        seed = generate_keypair().private_key
        first = AuditLogger(backend, InProcessSigner(seed))
        await first.initialize()
        for i in range(3):
            await first.log(event_type=f"e{i}", subject="s", actor_id="a", detail=f"d{i}")

        second = AuditLogger(backend, InProcessSigner(seed))
        await second.initialize()
        await second.log(event_type="e3", subject="s", actor_id="a", detail="d3")

        valid, last_seq = await second.verify_chain()
        assert valid is True
        assert last_seq == 4
