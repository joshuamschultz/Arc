"""Dual-signed rekey contract for a hosted machine after process restart."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arctrust.canonical import canonical_json
from arctrust.deployment_grant import DeploymentChallenge
from arctrust.signer import ED25519, verify_signature

_INTENT_DOMAIN = b"arc:hosted-machine-rekey-intent:v1\0"
_GRANT_DOMAIN = b"arc:hosted-machine-rekey-grant:v1\0"


class MachineRekeyError(RuntimeError):
    """A restarted machine cannot replace the current challenge authority."""


class MachineRekeyIntent(BaseModel):
    """New ephemeral machine key signs the exact prior journal revision."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    previous_head_scope: str = Field(pattern=r"^[A-Za-z0-9_/-]{8,256}$")
    previous_head_version: int = Field(ge=1)
    previous_head_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_challenge_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_epoch: int = Field(ge=1)
    next_epoch: int = Field(ge=1)
    challenge: DeploymentChallenge

    @model_validator(mode="after")
    def bounded_epoch(self) -> Self:
        if self.next_epoch not in {self.current_epoch, self.current_epoch + 1}:
            raise ValueError("machine rekey epoch did not advance safely")
        return self

    def canonical_bytes(self) -> bytes:
        """Encode all prior and next machine facts for one signature."""
        return canonical_json(self.model_dump(mode="json"))


class MachineRekeyGrant(BaseModel):
    """Cloud endorsement issued only after live payment and provider checks."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    intent: dict[str, Any]
    customer_id: str = Field(pattern=r"^[A-Za-z0-9_-]{3,128}$")
    subscription_id: str = Field(pattern=r"^[A-Za-z0-9_-]{3,128}$")
    purpose: Literal["machine-rekey"]

    def canonical_bytes(self) -> bytes:
        """Bind the machine's signed intent and independently checked customer."""
        return canonical_json(self.model_dump(mode="json"))


def _envelope(facts: BaseModel, sign: Callable[[bytes], bytes], domain: bytes) -> dict[str, Any]:
    message = domain + canonical_json(facts.model_dump(mode="json"))
    signature = sign(message)
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise MachineRekeyError("machine rekey signer returned invalid evidence")
    return {"facts": facts.model_dump(mode="json"), "signature": signature.hex()}


def sign_rekey_intent(
    intent: MachineRekeyIntent, sign: Callable[[bytes], bytes]
) -> dict[str, Any]:
    """Sign a pending rekey with only the new ephemeral machine key."""
    return _envelope(intent, sign, _INTENT_DOMAIN)


def verify_rekey_intent(
    envelope: Mapping[str, Any], *, expected: MachineRekeyIntent, now: int | None = None
) -> None:
    """Verify new-key possession and one exact previous journal revision."""
    try:
        if set(envelope) != {"facts", "signature"}:
            raise ValueError("invalid rekey envelope")
        facts = MachineRekeyIntent.model_validate(envelope["facts"])
        moment = int(time.time()) if now is None else now
        if (
            facts != expected
            or not facts.challenge.issued_at <= moment < facts.challenge.expires_at
            or not verify_signature(
                ED25519, _INTENT_DOMAIN + facts.canonical_bytes(),
                bytes.fromhex(envelope["signature"]),
                bytes.fromhex(facts.challenge.machine_public_key),
            )
        ):
            raise ValueError("rekey signature mismatch")
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise MachineRekeyError("machine rekey intent refused") from exc


def sign_rekey_grant(
    grant: MachineRekeyGrant, sign: Callable[[bytes], bytes], *, now: int | None = None
) -> dict[str, Any]:
    """Cloud-sign one verified machine intent after live external checks."""
    try:
        intent = MachineRekeyIntent.model_validate(grant.intent["facts"])
        verify_rekey_intent(grant.intent, expected=intent, now=now)
    except (KeyError, TypeError, ValueError, MachineRekeyError) as exc:
        raise MachineRekeyError("unverified machine rekey intent") from exc
    return _envelope(grant, sign, _GRANT_DOMAIN)


def verify_rekey_grant(
    envelope: Mapping[str, Any], *, issuer_public_key: bytes,
    expected: MachineRekeyGrant, now: int | None = None,
) -> None:
    """Require exact cloud and new-machine signatures over the current head."""
    try:
        if set(envelope) != {"facts", "signature"} or len(issuer_public_key) != 32:
            raise ValueError("invalid rekey grant envelope")
        facts = MachineRekeyGrant.model_validate(envelope["facts"])
        if facts != expected:
            raise ValueError("rekey grant facts changed")
        intent = MachineRekeyIntent.model_validate(facts.intent["facts"])
        verify_rekey_intent(facts.intent, expected=intent, now=now)
        if not verify_signature(
            ED25519, _GRANT_DOMAIN + facts.canonical_bytes(),
            bytes.fromhex(envelope["signature"]), issuer_public_key,
        ):
            raise ValueError("rekey issuer signature mismatch")
    except (TypeError, ValueError, KeyError, AttributeError, MachineRekeyError) as exc:
        raise MachineRekeyError("machine rekey grant refused") from exc
