"""Signed control revisions and the actor proofs that request them.

A control artifact is an instruction that runs later without a human present: a
schedule or a pulse check. Its definition lives in an agent-writable file, so the
file alone can never be what authorizes it. Each revision is approved by the
operator signing capability and bound to the exact definition bytes; each change
is requested by an actor (the agent itself, or the operator) whose fresh,
domain-separated proof names the definition it asks for.

This module is the shared contract. :mod:`arctrust.control_authority` is the
zero-config local custodian that issues and checks these revisions.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from arctrust.signer import Signer

ControlPurpose = Literal["schedule", "pulse"]

CONTROL_PURPOSES: frozenset[str] = frozenset({"schedule", "pulse"})
"""Every value :data:`ControlPurpose` admits, for checks on untyped input."""

_REVISION_DOMAIN = b"arc.control-revision.v1\n"
_PROOF_DOMAIN = b"arc.control-actor-proof.v1\n"
_PROOF_KEYS = frozenset(
    {
        "actor_did",
        "purpose",
        "artifact_id",
        "definition_digest",
        "issued_at",
        "nonce",
        "algorithm",
        "public_key",
        "signature",
    }
)
MAX_PROOF_BYTES = 4096


class ControlArtifactRefusedError(RuntimeError):
    """A control revision was revoked, stale, or did not bind its definition."""


class ControlArtifactUnavailableError(RuntimeError):
    """The protected control revision could not be checked or advanced."""


class SignedControlRevision(BaseModel):
    """Broker-custodied signed approval for exactly one definition revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1)
    agent_did: str = Field(min_length=1)
    purpose: ControlPurpose
    artifact_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    definition_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor_did: str = Field(min_length=1)
    issued_at: datetime
    revoked: bool = False
    signature: str = Field(pattern=r"^(?:[0-9a-f]{2})+$")

    @field_validator("issued_at")
    @classmethod
    def _aware_issue_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("signed control issue time must include a timezone")
        return value


class ControlActorProof(BaseModel):
    """A parsed actor proof; its signature is checked by the authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_did: str = Field(min_length=1, max_length=512)
    purpose: ControlPurpose
    artifact_id: str = Field(min_length=1, max_length=512)
    definition_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: datetime
    nonce: str = Field(pattern=r"^[0-9a-f]{32}$")
    algorithm: str = Field(min_length=1, max_length=64)
    public_key: str = Field(pattern=r"^(?:[0-9a-f]{2}){1,512}$")
    signature: str = Field(pattern=r"^(?:[0-9a-f]{2}){1,512}$")

    @field_validator("issued_at")
    @classmethod
    def _aware_issue_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("actor proof issue time must include a timezone")
        return value

    def signed_message(self) -> bytes:
        """The exact bytes the actor signed."""
        unsigned = self.model_dump(mode="json", exclude={"signature"})
        return _PROOF_DOMAIN + _canonical(unsigned)


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def control_revision_message(revision: SignedControlRevision) -> bytes:
    """The domain-separated bytes the operator signs for one revision."""
    return _REVISION_DOMAIN + _canonical(revision.model_dump(mode="json", exclude={"signature"}))


def sign_control_actor_proof(
    signer: Signer,
    *,
    actor_did: str,
    purpose: ControlPurpose,
    artifact_id: str,
    definition: bytes,
    issued_at: datetime,
) -> bytes:
    """Return a fresh, single-use proof that ``actor_did`` asks for ``definition``.

    ``signer`` is a signing capability, never key material. The proof names the
    exact definition digest and carries a random nonce, so it authorizes one
    registration of one definition and cannot be replayed or repurposed.
    """
    unsigned = ControlActorProof(
        actor_did=actor_did,
        purpose=purpose,
        artifact_id=artifact_id,
        definition_digest=hashlib.sha256(definition).hexdigest(),
        issued_at=issued_at,
        nonce=os.urandom(16).hex(),
        algorithm=signer.algorithm,
        public_key=bytes(signer.public_key).hex(),
        signature="00",
    )
    signature = signer.sign(unsigned.signed_message()).hex()
    return unsigned.model_copy(update={"signature": signature}).model_dump_json().encode("utf-8")


def parse_control_actor_proof(proof: bytes) -> ControlActorProof:
    """Parse an actor proof's shape; raise :class:`ControlArtifactRefusedError` if malformed."""
    if type(proof) is not bytes or not proof or len(proof) > MAX_PROOF_BYTES:
        raise ControlArtifactRefusedError("control actor proof is malformed")
    try:
        raw = json.loads(proof)
        if not isinstance(raw, dict) or set(raw) != _PROOF_KEYS:
            raise ValueError("shape")
        return ControlActorProof.model_validate(raw)
    except ValueError as exc:
        raise ControlArtifactRefusedError("control actor proof is malformed") from exc


__all__ = [
    "CONTROL_PURPOSES",
    "MAX_PROOF_BYTES",
    "ControlActorProof",
    "ControlArtifactRefusedError",
    "ControlArtifactUnavailableError",
    "ControlPurpose",
    "SignedControlRevision",
    "control_revision_message",
    "parse_control_actor_proof",
    "sign_control_actor_proof",
]
