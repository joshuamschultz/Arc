"""An encrypted external CAS head resumes grants and fences expired challenges."""

from __future__ import annotations

import base64
import hashlib

import pytest
from nacl.signing import SigningKey
from packages.arctrust.tests.test_hosted_claim import Anchor, Audit, Authority, Users

from arctrust.deployment_grant import DeploymentChallenge, DeploymentGrant, sign_deployment_grant
from arctrust.hosted_claim import HostedClaimError, HostedFirstClaim
from arctrust.hosted_journal import HostedClaimJournal, HostedJournalError
from arctrust.monotonic import AnchorHead


class Cipher:
    def seal(self, payload: bytes) -> str:
        return base64.b64encode(payload).decode()

    def open(self, sealed: str) -> bytes:
        return base64.b64decode(sealed)


def _new() -> tuple[
    HostedClaimJournal, HostedFirstClaim, SigningKey, SigningKey, Users, object, Audit
]:
    machine, issuer = SigningKey.generate(), SigningKey.generate()
    challenge = DeploymentChallenge(
        order_id="order_123456",
        server_id=123,
        domain="first.example.com",
        release_id="release-1",
        arc_image_digest="a" * 64,
        machine_public_key=bytes(machine.verify_key).hex(),
        nonce="n" * 32,
        issued_at=1_790_000_000,
        expires_at=1_790_000_120,
    )
    journal = HostedClaimJournal(Anchor(), Cipher())
    journal.initialize(challenge)
    users, proof, audit = Users(), object(), Audit()
    service = HostedFirstClaim(
        challenge=challenge,
        expected_epoch=1,
        issuer_public_key=bytes(issuer.verify_key),
        anchor=journal,
        authority=Authority(users, proof),
        actor_proof=proof,
        tenant_id="acme",
        audit_sink=audit,
        machine_signer=lambda data: machine.sign(data).signature,
    )
    service.arm()
    return journal, service, machine, issuer, users, proof, audit


def test_restart_recovers_same_sealed_grant_and_completes_once() -> None:
    journal, first, machine, issuer, users, proof, audit = _new()
    challenge, epoch, _ = journal.current()
    grant = DeploymentGrant(
        **challenge.model_dump(),
        customer_id="cus_123",
        subscription_id="sub_123",
        customer_claim_id="claim_123456",
        customer_email="customer@example.com",
        customer_claim_secret_sha256=hashlib.sha256(("s" * 43).encode()).hexdigest(),
        claim_expires_at=1_790_086_400,
        epoch=epoch,
        purpose="initial-claim",
    )
    first.install_grant(
        sign_deployment_grant(grant, lambda data: issuer.sign(data).signature),
        now=1_790_000_030,
    )
    assert journal.latest().intent == "granted:claim_123456"
    assert isinstance(journal._anchor.latest(), AnchorHead)
    assert "customer@example.com" not in journal._anchor.latest().intent
    second = HostedFirstClaim(
        challenge=challenge,
        expected_epoch=epoch,
        issuer_public_key=bytes(issuer.verify_key),
        anchor=journal,
        authority=Authority(users, proof),
        actor_proof=proof,
        tenant_id="acme",
        audit_sink=audit,
        machine_signer=lambda data: machine.sign(data).signature,
    )
    assert second.status() == "awaiting_setup"
    second.claim("s" * 43, "valid-password-value", now=1_790_000_600)
    assert second.status() == "setup_complete"
    assert journal.current()[2] is None
    with pytest.raises(HostedClaimError):
        second.claim("s" * 43, "valid-password-value", now=1_790_000_600)


def test_expired_unclaimed_challenge_rotates_nonce_and_epoch_after_restart() -> None:
    journal, _first, machine, issuer, users, proof, audit = _new()
    challenge, epoch, _ = journal.current()
    restarted = HostedFirstClaim(
        challenge=challenge,
        expected_epoch=epoch,
        issuer_public_key=bytes(issuer.verify_key),
        anchor=journal,
        authority=Authority(users, proof),
        actor_proof=proof,
        tenant_id="acme",
        audit_sink=audit,
        machine_signer=lambda data: machine.sign(data).signature,
    )
    signed = restarted.signed_challenge(now=1_790_000_600)
    current, epoch, grant = journal.current()
    assert epoch == 1 and grant is None
    assert signed["facts"]["nonce"] != challenge.nonce
    assert current.expires_at == 1_790_000_900
    assert journal.latest().intent == "challenge"


def test_journal_refuses_premature_rotation_and_claim_id_substitution() -> None:
    journal, first, _machine, issuer, _users, _proof, _audit = _new()
    challenge, epoch, _ = journal.current()
    newer = challenge.model_copy(
        update={
            "nonce": "x" * 32,
            "issued_at": 1_790_000_060,
            "expires_at": 1_790_000_360,
        }
    )
    with pytest.raises(HostedJournalError, match="not expired"):
        journal.rotate_challenge(newer, epoch=epoch, now=1_790_000_060)
    grant = DeploymentGrant(
        **challenge.model_dump(),
        customer_id="cus_123",
        subscription_id="sub_123",
        customer_claim_id="claim_123456",
        customer_email="customer@example.com",
        customer_claim_secret_sha256=hashlib.sha256(("s" * 43).encode()).hexdigest(),
        claim_expires_at=1_790_086_400,
        epoch=epoch,
        purpose="initial-claim",
    )
    first.install_grant(
        sign_deployment_grant(grant, lambda data: issuer.sign(data).signature),
        now=1_790_000_030,
    )
    head = journal.latest()
    with pytest.raises(HostedJournalError, match="transition"):
        journal.compare_and_advance(head, head.digest, "pending:other_claim")
    assert journal.latest() == head


def test_expired_grant_rotates_epoch_and_old_grant_cannot_be_reinstalled() -> None:
    journal, service, _machine, issuer, _users, _proof, _audit = _new()
    challenge, epoch, _ = journal.current()
    grant = DeploymentGrant(
        **challenge.model_dump(),
        customer_id="cus_123",
        subscription_id="sub_123",
        customer_claim_id="claim_123456",
        customer_email="customer@example.com",
        customer_claim_secret_sha256=hashlib.sha256(("s" * 43).encode()).hexdigest(),
        claim_expires_at=1_790_000_300,
        epoch=epoch,
        purpose="initial-claim",
    )
    envelope = sign_deployment_grant(grant, lambda data: issuer.sign(data).signature)
    service.install_grant(envelope, now=1_790_000_030)
    signed = service.signed_challenge(now=1_790_000_301)
    assert signed["facts"]["nonce"] != challenge.nonce
    assert journal.current()[1] == 2
    assert journal.current()[2] is None
    with pytest.raises(HostedClaimError):
        service.install_grant(envelope, now=1_790_000_301)


def test_journal_refuses_tampered_scope_and_conflicting_cas_result() -> None:
    challenge = _new()[0].current()[0]

    class WrongScope(Anchor):
        scope = "hosted/claim"

        def latest(self):
            head = super().latest()
            return head.model_copy(update={"scope": "other/scope"}) if head else None

    backing = WrongScope()
    journal = HostedClaimJournal(backing, Cipher())
    journal.initialize(challenge)
    with pytest.raises(HostedJournalError):
        journal.latest()

    class WrongCAS(Anchor):
        def compare_and_advance(self, expected, digest, intent):
            head = super().compare_and_advance(expected, digest, intent)
            return head.model_copy(update={"version": head.version + 1})

    with pytest.raises(HostedJournalError, match="conflicting evidence"):
        HostedClaimJournal(WrongCAS(), Cipher()).initialize(challenge)
