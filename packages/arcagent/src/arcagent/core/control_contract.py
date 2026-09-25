"""Neutral custody contract for scheduled and pulse control definitions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

ControlPurpose = Literal["schedule", "pulse"]
ControlActionProofSource = Callable[[ControlPurpose, str, bytes], Awaitable[bytes]]


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


class ControlArtifactAuthority(Protocol):
    """Externally custodied revision CAS and fresh dispatch verification."""

    async def register_revision(
        self,
        *,
        tenant_id: str,
        agent_did: str,
        purpose: ControlPurpose,
        artifact_id: str,
        canonical_definition: bytes,
        expected_revision: int | None,
        actor_proof: bytes,
    ) -> SignedControlRevision: ...

    async def verify_current(
        self,
        *,
        tenant_id: str,
        agent_did: str,
        purpose: ControlPurpose,
        artifact_id: str,
        canonical_definition: bytes,
        approval: SignedControlRevision,
        occurrence_id: str,
    ) -> None: ...
