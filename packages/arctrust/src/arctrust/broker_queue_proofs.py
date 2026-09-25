"""Signed lease and recovery facts for the hosted queue broker."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arctrust.canonical import canonical_json
from arctrust.signer import ED25519, verify_signature

_LEASE_DOMAIN = b"arc:broker-queue-lease:v1\0"
_RECOVERY_DOMAIN = b"arc:broker-queue-recovery:v1\0"
_EPOCH = r"^[1-9][0-9]{0,18}$"


class BrokerQueueProofError(RuntimeError):
    """A queue broker grant is unavailable, stale, or incorrectly scoped."""


class BrokerQueueLease(BaseModel):
    """Signed description of the current narrow machine queue capability."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    version: Literal[1] = 1
    tenant_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    journal_scope: str = Field(pattern=r"^[a-zA-Z0-9_/-]{1,256}$")
    machine_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    machine_public_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    tls_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    lease_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,128}$")
    active_owner_epoch: str = Field(pattern=_EPOCH)
    fenced_through_epoch: str = Field(pattern=r"^(?:0|[1-9][0-9]{0,18})$")
    issued_at: int = Field(gt=0)
    expires_at: int = Field(gt=0)
    next_sequence: int = Field(ge=1)
    allowed_purposes: tuple[
        Literal["anchor.read"],
        Literal["anchor.advance"],
        Literal["queue.recover"],
        Literal["record.seal"],
        Literal["record.open"],
    ] = Field(strict=False)

    @model_validator(mode="after")
    def bounded_holder(self) -> Self:
        if (
            int(self.fenced_through_epoch) >= int(self.active_owner_epoch)
            or self.expires_at <= self.issued_at
            or self.expires_at - self.issued_at > 3600
        ):
            raise ValueError("queue lease lifetime or owner fence invalid")
        return self

    def canonical_bytes(self) -> bytes:
        """Encode all lease facts as stable signature input."""
        return canonical_json(self.model_dump(mode="json"))


class BrokerQueueRecoveryProof(BaseModel):
    """Fresh issuer evidence for recovering one already fenced owner."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    version: Literal[1] = 1
    tenant_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    journal_scope: str = Field(pattern=r"^[a-zA-Z0-9_/-]{1,256}$")
    prior_owner_epoch: str = Field(pattern=_EPOCH)
    active_owner_epoch: str = Field(pattern=_EPOCH)
    lease_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,128}$")
    purpose: Literal["queue.recover"] = "queue.recover"
    nonce: str = Field(pattern=r"^[A-Za-z0-9_-]{32,128}$")
    issued_at: int = Field(gt=0)
    expires_at: int = Field(gt=0)

    @model_validator(mode="after")
    def bounded_recovery(self) -> Self:
        if (
            int(self.prior_owner_epoch) >= int(self.active_owner_epoch)
            or self.expires_at <= self.issued_at
            or self.expires_at - self.issued_at > 60
        ):
            raise ValueError("queue recovery epoch or lifetime invalid")
        return self

    def canonical_bytes(self) -> bytes:
        """Encode every recovery scope and fence as stable signature input."""
        return canonical_json(self.model_dump(mode="json"))


def _sign(
    facts: BrokerQueueLease | BrokerQueueRecoveryProof,
    sign: Callable[[bytes], bytes],
    domain: bytes,
) -> dict[str, Any]:
    signature = sign(domain + facts.canonical_bytes())
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise BrokerQueueProofError("queue broker issuer returned invalid signature")
    return {"facts": facts.model_dump(mode="json"), "signature": signature.hex()}


def sign_broker_queue_lease(
    lease: BrokerQueueLease, sign: Callable[[bytes], bytes]
) -> dict[str, Any]:
    """Sign one current lease through the broker's nonexportable issuer."""
    return _sign(lease, sign, _LEASE_DOMAIN)


def sign_broker_queue_recovery(
    proof: BrokerQueueRecoveryProof, sign: Callable[[bytes], bytes]
) -> dict[str, Any]:
    """Sign one short recovery fence through the broker's issuer."""
    return _sign(proof, sign, _RECOVERY_DOMAIN)


def _verify(
    envelope: Mapping[str, Any],
    *,
    expected: BrokerQueueLease | BrokerQueueRecoveryProof,
    issuer_public_key: bytes,
    domain: bytes,
    now: int | None,
) -> None:
    try:
        if set(envelope) != {"facts", "signature"} or len(issuer_public_key) != 32:
            raise ValueError("invalid queue broker envelope")
        model_type = type(expected)
        facts = model_type.model_validate(envelope["facts"])
        moment = int(time.time()) if now is None else now
        if (
            type(moment) is not int
            or facts != expected
            or not facts.issued_at <= moment < facts.expires_at
            or not verify_signature(
                ED25519,
                domain + facts.canonical_bytes(),
                bytes.fromhex(envelope["signature"]),
                issuer_public_key,
            )
        ):
            raise ValueError("queue broker signature or facts mismatch")
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise BrokerQueueProofError("queue broker signed facts refused") from exc


def verify_broker_queue_lease(
    envelope: Mapping[str, Any],
    *,
    issuer_public_key: bytes,
    expected: BrokerQueueLease,
    now: int | None = None,
) -> None:
    """Verify one exact lease against an independently pinned broker issuer."""
    _verify(
        envelope,
        expected=expected,
        issuer_public_key=issuer_public_key,
        domain=_LEASE_DOMAIN,
        now=now,
    )


def verify_broker_queue_recovery(
    envelope: Mapping[str, Any],
    *,
    issuer_public_key: bytes,
    expected: BrokerQueueRecoveryProof,
    now: int | None = None,
) -> None:
    """Verify exact short recovery facts before authoritative CAS recheck."""
    _verify(
        envelope,
        expected=expected,
        issuer_public_key=issuer_public_key,
        domain=_RECOVERY_DOMAIN,
        now=now,
    )
