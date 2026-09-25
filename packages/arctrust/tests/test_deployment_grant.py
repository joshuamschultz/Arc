"""Hosted first claim binds independent paid, machine, and customer facts."""

from __future__ import annotations

import pytest
from nacl.signing import SigningKey

from arctrust.deployment_grant import (
    DeploymentChallenge,
    DeploymentGrant,
    DeploymentGrantError,
    sign_challenge,
    sign_deployment_grant,
    verify_challenge,
    verify_deployment_grant,
)


def _challenge(machine: SigningKey) -> DeploymentChallenge:
    return DeploymentChallenge(
        order_id="ord_12345",
        server_id=4512,
        domain="first.example.com",
        release_id="arc-2026-09-25",
        arc_image_digest="a" * 64,
        machine_public_key=bytes(machine.verify_key).hex(),
        nonce="n" * 32,
        issued_at=1_790_000_000,
        expires_at=1_790_000_120,
    )


def _grant(challenge: DeploymentChallenge) -> DeploymentGrant:
    return DeploymentGrant(
        **challenge.model_dump(),
        customer_id="cus_123",
        subscription_id="sub_123",
        customer_claim_id="claim_opaque_123",
        customer_email="customer@example.com",
        customer_claim_secret_sha256="d" * 64,
        claim_expires_at=1_790_086_400,
        epoch=1,
        purpose="initial-claim",
    )


def test_challenge_and_grant_bind_distinct_signers_and_all_facts() -> None:
    machine, issuer = SigningKey.generate(), SigningKey.generate()
    challenge = _challenge(machine)
    signed_challenge = sign_challenge(challenge, lambda data: machine.sign(data).signature)
    signed_grant = sign_deployment_grant(
        _grant(challenge), lambda data: issuer.sign(data).signature
    )
    verify_challenge(signed_challenge, expected=challenge, now=1_790_000_030)
    verify_deployment_grant(
        signed_grant,
        issuer_public_key=bytes(issuer.verify_key),
        expected=_grant(challenge),
        now=1_790_000_030,
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("order_id", "ord_other"),
        ("server_id", 9999),
        ("domain", "other.example.com"),
        ("release_id", "arc-other"),
        ("arc_image_digest", "c" * 64),
        ("nonce", "x" * 32),
    ],
)
def test_challenge_substitution_refused(field: str, replacement: str | int) -> None:
    machine = SigningKey.generate()
    challenge = _challenge(machine)
    signed = sign_challenge(challenge, lambda data: machine.sign(data).signature)
    with pytest.raises(DeploymentGrantError):
        verify_challenge(
            signed,
            expected=challenge.model_copy(update={field: replacement}),
            now=1_790_000_030,
        )


def test_grant_customer_substitution_and_replay_epoch_refused() -> None:
    machine, issuer = SigningKey.generate(), SigningKey.generate()
    grant = _grant(_challenge(machine))
    signed = sign_deployment_grant(grant, lambda data: issuer.sign(data).signature)
    for changed in (
        {"customer_id": "cus_other"},
        {"subscription_id": "sub_other"},
        {"customer_claim_id": "claim_other"},
        {"customer_email": "other@example.com"},
        {"customer_claim_secret_sha256": "e" * 64},
        {"epoch": 2},
        {"purpose": "operator-signing"},
    ):
        with pytest.raises(DeploymentGrantError):
            verify_deployment_grant(
                signed,
                issuer_public_key=bytes(issuer.verify_key),
                expected=grant.model_copy(update=changed),
                now=1_790_000_030,
            )


def test_expired_and_forged_grants_refused() -> None:
    machine, issuer = SigningKey.generate(), SigningKey.generate()
    grant = _grant(_challenge(machine))
    signed = sign_deployment_grant(grant, lambda data: issuer.sign(data).signature)
    with pytest.raises(DeploymentGrantError):
        verify_deployment_grant(
            signed,
            issuer_public_key=bytes(issuer.verify_key),
            expected=grant,
            now=1_790_000_121,
        )
    verify_deployment_grant(
        signed,
        issuer_public_key=bytes(issuer.verify_key),
        expected=grant,
        now=1_790_000_121,
        claim_lifetime=True,
    )
    with pytest.raises(DeploymentGrantError):
        verify_deployment_grant(
            signed,
            issuer_public_key=bytes(SigningKey.generate().verify_key),
            expected=grant,
            now=1_790_000_030,
        )
