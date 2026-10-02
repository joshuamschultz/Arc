"""Neutral custody contract for scheduled and pulse control definitions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypedDict, runtime_checkable

from arctrust import (
    ControlArtifactRefusedError,
    ControlArtifactUnavailableError,
    ControlPurpose,
    SignedControlRevision,
)

from arcagent.core.run_contract import RunTriggerIssuer

ControlActionProofSource = Callable[[ControlPurpose, str, bytes], Awaitable[bytes]]


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


@runtime_checkable
class ControlActorEnrollment(Protocol):
    """An authority that pins the key each agent proves its control requests with."""

    def enroll_actor(self, did: str, public_key: bytes, algorithm: str) -> None: ...


class ControlAgentKwargs(TypedDict, total=False):
    """The ``ArcAgent`` keyword arguments a :class:`ControlArtifactBinding` supplies."""

    control_artifact_authority: ControlArtifactAuthority
    control_tenant_id: str
    trigger_issuer: RunTriggerIssuer


@dataclass(frozen=True)
class ControlArtifactBinding:
    """One deployment's control authority, as every entry point hands it to agents.

    ``operator_proof`` signs an operator-role request (the dashboard); agents sign
    their own requests with their identity, so no per-agent source is carried.
    """

    authority: ControlArtifactAuthority
    tenant_id: str
    trigger_issuer: RunTriggerIssuer
    operator_proof: Callable[[ControlPurpose, str, bytes], bytes]

    def agent_kwargs(self) -> ControlAgentKwargs:
        """The ``ArcAgent`` keyword arguments that bind an agent to this authority."""
        return ControlAgentKwargs(
            control_artifact_authority=self.authority,
            control_tenant_id=self.tenant_id,
            trigger_issuer=self.trigger_issuer,
        )


__all__ = [
    "ControlActionProofSource",
    "ControlActorEnrollment",
    "ControlAgentKwargs",
    "ControlArtifactAuthority",
    "ControlArtifactBinding",
    "ControlArtifactRefusedError",
    "ControlArtifactUnavailableError",
    "ControlPurpose",
    "SignedControlRevision",
]
