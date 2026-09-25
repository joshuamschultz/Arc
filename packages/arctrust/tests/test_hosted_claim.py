"""A paid grant and customer secret create only one first operator."""

from __future__ import annotations

import hashlib

import pytest
from nacl.signing import SigningKey

from arctrust.deployment_grant import DeploymentChallenge, DeploymentGrant, sign_deployment_grant
from arctrust.hosted_claim import HostedClaimError, HostedFirstClaim
from arctrust.monotonic import AnchorHead
from arctrust.users import OPERATOR


class Anchor:
    scope = "hosted/claim"

    def __init__(self) -> None:
        self.head: AnchorHead | None = None

    def latest(self) -> AnchorHead | None:
        return self.head

    def compare_and_advance(self, expected: AnchorHead | None, digest: str, intent: str) -> AnchorHead:
        if self.head != expected:
            raise RuntimeError("stale")
        self.head = AnchorHead(
            scope=self.scope,
            version=1 if expected is None else expected.version + 1,
            digest=digest,
            previous_digest=expected.digest if expected else None,
            intent=intent,
        )
        return self.head


class Users:
    def __init__(self) -> None:
        self.records: dict[str, object] = {}
        self.fail_after_write = False

    def is_empty(self) -> bool:
        return not self.records

    def get(self, email: str):
        return self.records.get(email)

    def claim_first_operator(self, email: str, password: str, *, org: str, claim_digest: str):
        assert password == "valid-password-value"
        user = type("User", (), {
            "email": email, "did": f"did:arc:{org}:user/first",
            "roles": (OPERATOR,), "initial_claim_digest": claim_digest,
        })()
        self.records[email] = user
        if self.fail_after_write:
            raise RuntimeError("uncertain write")
        return user


class Authority:
    def __init__(self, users: Users, proof: object) -> None:
        self.users = users
        self.proof = proof

    def user_store(self, proof: object) -> Users:
        if proof is not self.proof:
            raise PermissionError
        return self.users


class Audit:
    def __init__(self) -> None:
        self.events = []
        self.fail = False

    def write_durable(self, event) -> None:
        if self.fail:
            raise RuntimeError("audit unavailable")
        self.events.append(event)


def _service() -> tuple[HostedFirstClaim, Anchor, Users, dict[str, object], Audit]:
    issuer, machine = SigningKey.generate(), SigningKey.generate()
    secret = "customer-browser-secret-256-bits"
    challenge = DeploymentChallenge(
        order_id="order_123456", server_id=123, domain="first.example.com",
        release_id="arc-2026-09-25", arc_image_digest="a" * 64,
        machine_public_key=bytes(machine.verify_key).hex(), nonce="n" * 32,
        issued_at=1_790_000_000, expires_at=1_790_000_120,
    )
    grant = DeploymentGrant(
        **challenge.model_dump(), customer_id="cus_123", subscription_id="sub_123",
        customer_claim_id="claim_123456", customer_email="customer@example.com",
        customer_claim_secret_sha256=hashlib.sha256(secret.encode()).hexdigest(),
        claim_expires_at=1_790_086_400,
        epoch=1, purpose="initial-claim",
    )
    anchor, users, proof = Anchor(), Users(), object()
    audit = Audit()
    service = HostedFirstClaim(
        challenge=challenge, expected_epoch=1, issuer_public_key=bytes(issuer.verify_key),
        anchor=anchor, authority=Authority(users, proof), actor_proof=proof, tenant_id="acme",
        audit_sink=audit,
        machine_signer=lambda data: machine.sign(data).signature,
    )
    service.arm()
    envelope = sign_deployment_grant(grant, lambda data: issuer.sign(data).signature)
    service.install_grant(envelope, now=1_790_000_030)
    return service, anchor, users, envelope, audit


def test_claim_creates_operator_once_and_replay_is_refused() -> None:
    service, anchor, users, _, _audit = _service()
    user = service.claim("customer-browser-secret-256-bits", "valid-password-value", now=1_790_000_030)
    assert user.email == "customer@example.com"
    assert user.roles == (OPERATOR,)
    assert anchor.latest().intent == "claimed:claim_123456"
    with pytest.raises(HostedClaimError):
        service.claim("customer-browser-secret-256-bits", "valid-password-value", now=1_790_000_030)
    assert len(users.records) == 1


def test_wrong_browser_secret_refused_without_reservation() -> None:
    service, anchor, users, _, _audit = _service()
    with pytest.raises(HostedClaimError):
        service.claim("wrong", "valid-password-value", now=1_790_000_030)
    assert anchor.latest().intent == "challenge"
    assert users.is_empty()


def test_customer_can_finish_after_short_machine_challenge_expires() -> None:
    service, anchor, users, _, _audit = _service()
    user = service.claim(
        "customer-browser-secret-256-bits", "valid-password-value", now=1_790_000_600
    )
    assert user.email == "customer@example.com"
    assert anchor.latest().intent == "claimed:claim_123456"
    assert len(users.records) == 1


def test_uncertain_account_write_reconciles_exact_pending_grant() -> None:
    service, anchor, users, _, _audit = _service()
    users.fail_after_write = True
    with pytest.raises(RuntimeError):
        service.claim("customer-browser-secret-256-bits", "valid-password-value", now=1_790_000_030)
    assert anchor.latest().intent == "pending:claim_123456"
    users.fail_after_write = False
    service.claim("customer-browser-secret-256-bits", "valid-password-value", now=1_790_000_030)
    assert anchor.latest().intent == "claimed:claim_123456"


def test_pending_account_from_wrong_tenant_is_refused() -> None:
    service, anchor, users, _, _audit = _service()
    users.fail_after_write = True
    with pytest.raises(RuntimeError):
        service.claim("customer-browser-secret-256-bits", "valid-password-value", now=1_790_000_030)
    account = users.records["customer@example.com"]
    account.did = "did:arc:other:user/first"
    users.fail_after_write = False
    with pytest.raises(HostedClaimError, match="conflicts"):
        service.claim("customer-browser-secret-256-bits", "valid-password-value", now=1_790_000_030)
    assert anchor.latest().intent == "pending:claim_123456"


def test_audit_failure_does_not_activate_grant_or_reserve_claim() -> None:
    service, anchor, users, envelope, audit = _service()
    service._grant_envelope = None
    audit.fail = True
    with pytest.raises(RuntimeError, match="audit"):
        service.install_grant(envelope, now=1_790_000_030)
    assert service.status() == "awaiting_grant"
    audit.fail = False
    service.install_grant(envelope, now=1_790_000_030)
    audit.fail = True
    with pytest.raises(RuntimeError, match="audit"):
        service.claim("customer-browser-secret-256-bits", "valid-password-value", now=1_790_000_030)
    assert anchor.latest().intent == "challenge"
    assert users.is_empty()
