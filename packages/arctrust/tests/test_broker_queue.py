"""Broker lease and recovery proofs bind one machine, root, and owner epoch."""

from __future__ import annotations

import time

import pytest
from nacl.signing import SigningKey

import arctrust


def _signer() -> tuple[SigningKey, bytes]:
    key = SigningKey.generate()
    return key, bytes(key.verify_key)


def test_signed_lease_exact_scope_and_issuer() -> None:
    key, public = _signer()
    now = int(time.time())
    lease = arctrust.BrokerQueueLease(
        tenant_id="acme",
        journal_scope="queues/acme",
        machine_id="server-123",
        machine_public_key="a" * 64,
        tls_fingerprint="b" * 64,
        lease_id="lease-12345678",
        active_owner_epoch="2",
        fenced_through_epoch="1",
        issued_at=now - 1,
        expires_at=now + 60,
        next_sequence=7,
        allowed_purposes=["anchor.read", "anchor.advance", "queue.recover"],
    )
    envelope = arctrust.sign_broker_queue_lease(lease, lambda payload: key.sign(payload).signature)
    arctrust.verify_broker_queue_lease(envelope, issuer_public_key=public, expected=lease, now=now)
    with pytest.raises(arctrust.BrokerQueueProofError):
        arctrust.verify_broker_queue_lease(
            envelope,
            issuer_public_key=public,
            expected=lease.model_copy(update={"journal_scope": "queues/other"}),
            now=now,
        )
    with pytest.raises(arctrust.BrokerQueueProofError):
        arctrust.verify_broker_queue_lease(
            envelope,
            issuer_public_key=bytes(SigningKey.generate().verify_key),
            expected=lease,
            now=now,
        )


def test_recovery_proof_refuses_live_or_expired_owner() -> None:
    key, public = _signer()
    now = int(time.time())
    proof = arctrust.BrokerQueueRecoveryProof(
        tenant_id="acme",
        journal_scope="queues/acme",
        prior_owner_epoch="1",
        active_owner_epoch="2",
        lease_id="lease-12345678",
        nonce="n" * 32,
        issued_at=now - 1,
        expires_at=now + 59,
    )
    envelope = arctrust.sign_broker_queue_recovery(
        proof, lambda payload: key.sign(payload).signature
    )
    arctrust.verify_broker_queue_recovery(
        envelope, issuer_public_key=public, expected=proof, now=now
    )
    with pytest.raises(arctrust.BrokerQueueProofError):
        arctrust.verify_broker_queue_recovery(
            envelope,
            issuer_public_key=public,
            expected=proof,
            now=now + 60,
        )
    with pytest.raises(ValueError):
        arctrust.BrokerQueueRecoveryProof(
            tenant_id="acme",
            journal_scope="queues/acme",
            prior_owner_epoch="2",
            active_owner_epoch="2",
            lease_id="lease-12345678",
            nonce="n" * 32,
            issued_at=now,
            expires_at=now + 60,
        )
