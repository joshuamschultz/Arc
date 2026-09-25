"""Signed, bounded first-claim facts for a hosted Arc deployment.

Payment, provider, and email verification belong to the cloud issuer. ArcTrust
authenticates only their exact, independently supplied binding and signatures.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arctrust.canonical import canonical_json
from arctrust.signer import ED25519, verify_signature

_CHALLENGE_DOMAIN = b"arc:hosted-machine-challenge:v1\0"
_GRANT_DOMAIN = b"arc:hosted-first-claim-grant:v1\0"


class DeploymentGrantError(RuntimeError):
    """A hosted machine or customer claim cannot be authenticated."""


class DeploymentChallenge(BaseModel):
    """Machine signed evidence over the order's nonce and immutable boot facts."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    order_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")
    server_id: int = Field(gt=0)
    domain: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,251}[a-z0-9]$")
    release_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    arc_image_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    machine_public_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    nonce: str = Field(min_length=32, max_length=128)
    issued_at: int = Field(gt=0)
    expires_at: int = Field(gt=0)

    @model_validator(mode="after")
    def bounded_lifetime(self) -> Self:
        if self.expires_at <= self.issued_at or self.expires_at - self.issued_at > 300:
            raise ValueError("hosted challenge lifetime is invalid")
        if ".." in self.domain or "." not in self.domain:
            raise ValueError("hosted challenge domain is invalid")
        return self

    def canonical_bytes(self) -> bytes:
        """Encode every challenge fact in stable, signable JSON bytes."""
        return canonical_json(self.model_dump(mode="json"))


class DeploymentGrant(DeploymentChallenge):
    """Issuer signed admission bound to separate customer proof and machine."""

    customer_id: str = Field(pattern=r"^[A-Za-z0-9_-]{3,128}$")
    subscription_id: str = Field(pattern=r"^[A-Za-z0-9_-]{3,128}$")
    customer_claim_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")
    customer_email: str = Field(min_length=3, max_length=254)
    customer_claim_secret_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_expires_at: int = Field(gt=0)
    epoch: int = Field(ge=1)
    purpose: Literal["initial-claim"]

    @model_validator(mode="after")
    def valid_customer_email(self) -> Self:
        if (
            self.customer_email != self.customer_email.lower()
            or self.customer_email.count("@") != 1
            or any(char.isspace() for char in self.customer_email)
            or not self.expires_at <= self.claim_expires_at <= self.issued_at + 7 * 86400
        ):
            raise ValueError("hosted customer claim is invalid")
        return self


def _sign(
    model: DeploymentChallenge, sign: Callable[[bytes], bytes], domain: bytes
) -> dict[str, Any]:
    signature = sign(domain + model.canonical_bytes())
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise DeploymentGrantError("hosted signer returned an invalid signature")
    return {"facts": model.model_dump(mode="json"), "signature": signature.hex()}


def sign_challenge(
    challenge: DeploymentChallenge, sign: Callable[[bytes], bytes]
) -> dict[str, Any]:
    """Sign one challenge with the boot-generated ephemeral machine key."""
    return _sign(challenge, sign, _CHALLENGE_DOMAIN)


def sign_deployment_grant(
    grant: DeploymentGrant, sign: Callable[[bytes], bytes]
) -> dict[str, Any]:
    """Sign one first-claim grant through a nonexportable issuer capability."""
    return _sign(grant, sign, _GRANT_DOMAIN)


def _verify(
    envelope: Mapping[str, Any],
    *,
    model_type: type[DeploymentChallenge],
    expected: DeploymentChallenge,
    public_key: bytes,
    domain: bytes,
    now: int | None,
    claim_lifetime: bool = False,
) -> None:
    try:
        if set(envelope) != {"facts", "signature"} or len(public_key) != 32:
            raise ValueError("invalid signed envelope")
        facts = model_type.model_validate(envelope["facts"])
        signature = bytes.fromhex(envelope["signature"])
        moment = int(time.time()) if now is None else now
        if (
            type(moment) is not int
            or facts != expected
            or not facts.issued_at <= moment < (
                facts.claim_expires_at
                if claim_lifetime and isinstance(facts, DeploymentGrant)
                else facts.expires_at
            )
            or not verify_signature(
                ED25519, domain + facts.canonical_bytes(), signature, public_key
            )
        ):
            raise ValueError("hosted signed facts refused")
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise DeploymentGrantError("hosted signed facts unavailable or invalid") from exc


def verify_challenge(
    envelope: Mapping[str, Any], *, expected: DeploymentChallenge, now: int | None = None
) -> None:
    """Require the exact independently known nonce, machine, domain, and image."""
    _verify(
        envelope,
        model_type=DeploymentChallenge,
        expected=expected,
        public_key=bytes.fromhex(expected.machine_public_key),
        domain=_CHALLENGE_DOMAIN,
        now=now,
    )


def verify_deployment_grant(
    envelope: Mapping[str, Any],
    *,
    issuer_public_key: bytes,
    expected: DeploymentGrant,
    now: int | None = None,
    claim_lifetime: bool = False,
) -> None:
    """Require the exact first-claim facts against a separately pinned issuer."""
    _verify(
        envelope,
        model_type=DeploymentGrant,
        expected=expected,
        public_key=issuer_public_key,
        domain=_GRANT_DOMAIN,
        now=now,
        claim_lifetime=claim_lifetime,
    )
