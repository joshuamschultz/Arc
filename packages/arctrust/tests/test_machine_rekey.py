"""A restarted hosted machine can rebind only its own current claim head."""

from __future__ import annotations

import hashlib
import time

import pytest
from packages.arctrust.tests.test_hosted_journal import _new

import arctrust
from arctrust.deployment_grant import DeploymentGrant, sign_deployment_grant
from arctrust.hosted_claim import HostedClaimError
from arctrust.hosted_journal import HostedJournalError
from arctrust.machine_rekey import (
    MachineRekeyError,
    MachineRekeyGrant,
    MachineRekeyIntent,
    sign_rekey_grant,
    sign_rekey_intent,
    verify_rekey_grant,
    verify_rekey_intent,
)


def _facts():
    machine = arctrust.generate_keypair()
    issuer = arctrust.generate_keypair()
    now = int(time.time())
    challenge = arctrust.DeploymentChallenge(
        order_id="order_12345678", server_id=42, domain="first.arc.example.com",
        release_id="release-one", arc_image_digest="a" * 64,
        machine_public_key=machine.public_key.hex(), nonce="n" * 32,
        issued_at=now, expires_at=now + 300,
    )
    intent = MachineRekeyIntent(
        previous_head_scope="hosted/claim/order_12345678",
        previous_head_version=5, previous_head_digest="b" * 64,
        previous_challenge_digest=hashlib.sha256(b"previous challenge").hexdigest(),
        current_epoch=1, next_epoch=2, challenge=challenge,
    )
    signed = sign_rekey_intent(intent, lambda message: arctrust.sign(message, machine.private_key))
    grant = MachineRekeyGrant(
        intent=signed, customer_id="cus_123", subscription_id="sub_123",
        purpose="machine-rekey",
    )
    envelope = sign_rekey_grant(
        grant, lambda message: arctrust.sign(message, issuer.private_key)
    )
    return intent, signed, grant, envelope, machine, issuer


def test_rekey_requires_both_new_machine_and_cloud_issuer_signatures() -> None:
    intent, signed, grant, envelope, machine, issuer = _facts()
    verify_rekey_intent(signed, expected=intent, now=intent.challenge.issued_at)
    verify_rekey_grant(
        envelope, issuer_public_key=issuer.public_key, expected=grant,
        now=intent.challenge.issued_at,
    )
    wrong = intent.model_copy(update={"previous_head_digest": "c" * 64})
    with pytest.raises(MachineRekeyError):
        verify_rekey_intent(signed, expected=wrong, now=intent.challenge.issued_at)
    with pytest.raises(MachineRekeyError):
        verify_rekey_grant(
            envelope, issuer_public_key=machine.public_key, expected=grant,
            now=intent.challenge.issued_at,
        )
    with pytest.raises(MachineRekeyError):
        verify_rekey_grant(
            envelope, issuer_public_key=issuer.public_key, expected=grant,
            now=intent.challenge.expires_at,
        )


def test_rekey_refuses_claimed_epoch_and_tampered_nested_intent() -> None:
    intent, signed, grant, envelope, _machine, issuer = _facts()
    with pytest.raises(ValueError):
        MachineRekeyIntent.model_validate({
            **intent.model_dump(mode="json"), "next_epoch": 4,
        })
    altered = grant.model_copy(update={
        "intent": {**signed, "signature": "0" * 128},
    })
    with pytest.raises(MachineRekeyError):
        verify_rekey_grant(
            envelope, issuer_public_key=issuer.public_key, expected=altered,
            now=intent.challenge.issued_at,
        )


def test_journal_rekeys_current_unclaimed_head_and_fences_replay() -> None:
    journal, _service, _old_machine, issuer, _users, _proof, audit = _new()
    old_challenge, old_epoch, _ = journal.current()
    head = journal.latest()
    assert head is not None
    new_machine = arctrust.generate_keypair()
    moment = old_challenge.issued_at + 30
    challenge = old_challenge.model_copy(update={
        "machine_public_key": new_machine.public_key.hex(),
        "nonce": "r" * 32, "issued_at": moment, "expires_at": moment + 300,
    })
    intent = MachineRekeyIntent(
        previous_head_scope=head.scope, previous_head_version=head.version,
        previous_head_digest=head.digest,
        previous_challenge_digest=hashlib.sha256(old_challenge.canonical_bytes()).hexdigest(),
        current_epoch=old_epoch, next_epoch=old_epoch, challenge=challenge,
    )
    signed = sign_rekey_intent(
        intent, lambda message: arctrust.sign(message, new_machine.private_key)
    )
    grant = MachineRekeyGrant(
        intent=signed, customer_id="cus_123", subscription_id="sub_123",
        purpose="machine-rekey",
    )
    envelope = sign_rekey_grant(
        grant, lambda message: issuer.sign(message).signature, now=moment
    )
    with pytest.raises(HostedJournalError):
        journal.rekey_challenge(
            {**envelope, "signature": "0" * 128},
            issuer_public_key=bytes(issuer.verify_key), tenant_id="acme",
            audit_sink=audit, now=moment,
        )
    assert audit.events[-1].outcome == "deny"
    assert journal.latest() == head
    journal.rekey_challenge(
        envelope, issuer_public_key=bytes(issuer.verify_key), tenant_id="acme",
        audit_sink=audit, now=moment,
    )
    assert journal.current() == (challenge, old_epoch, None)
    assert audit.events[-1].action == "hosted.claim.rekey"
    with pytest.raises(HostedJournalError):
        journal.rekey_challenge(
            envelope, issuer_public_key=bytes(issuer.verify_key), tenant_id="acme",
            audit_sink=audit, now=moment,
        )
    assert audit.events[-1].outcome == "deny"


def test_rekey_fences_unexpired_grant_and_audit_failure_cannot_advance() -> None:
    journal, service, _old_machine, issuer, _users, _proof, audit = _new()
    old_challenge, old_epoch, _ = journal.current()
    moment = old_challenge.issued_at + 30
    old_grant = DeploymentGrant(
        **old_challenge.model_dump(), customer_id="cus_123", subscription_id="sub_123",
        customer_claim_id="claim_123456", customer_email="customer@example.com",
        customer_claim_secret_sha256=hashlib.sha256(("s" * 43).encode()).hexdigest(),
        claim_expires_at=old_challenge.issued_at + 86400, epoch=old_epoch,
        purpose="initial-claim",
    )
    service.install_grant(
        sign_deployment_grant(old_grant, lambda message: issuer.sign(message).signature),
        now=moment,
    )
    head = journal.latest()
    assert head is not None
    new_machine = arctrust.generate_keypair()
    challenge = old_challenge.model_copy(update={
        "machine_public_key": new_machine.public_key.hex(),
        "nonce": "r" * 32, "issued_at": moment, "expires_at": moment + 300,
    })
    intent = MachineRekeyIntent(
        previous_head_scope=head.scope, previous_head_version=head.version,
        previous_head_digest=head.digest,
        previous_challenge_digest=hashlib.sha256(old_challenge.canonical_bytes()).hexdigest(),
        current_epoch=old_epoch, next_epoch=old_epoch + 1, challenge=challenge,
    )
    signed = sign_rekey_intent(
        intent, lambda message: arctrust.sign(message, new_machine.private_key)
    )
    grant = MachineRekeyGrant(
        intent=signed, customer_id="cus_123", subscription_id="sub_123",
        purpose="machine-rekey",
    )
    envelope = sign_rekey_grant(
        grant, lambda message: issuer.sign(message).signature, now=moment
    )
    audit.fail = True
    with pytest.raises(RuntimeError, match="audit unavailable"):
        journal.rekey_challenge(
            envelope, issuer_public_key=bytes(issuer.verify_key), tenant_id="acme",
            audit_sink=audit, now=moment,
        )
    assert journal.latest() == head
    audit.fail = False
    journal.rekey_challenge(
        envelope, issuer_public_key=bytes(issuer.verify_key), tenant_id="acme",
        audit_sink=audit, now=moment,
    )
    assert journal.current() == (challenge, old_epoch + 1, None)
    with pytest.raises(HostedClaimError):
        service.claim("s" * 43, "valid-password-value", now=moment)
